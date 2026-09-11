import csv
import io
import re
from typing import List, Dict, Any, Tuple
import openpyxl
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from apps.contacts.models import Contact, ContactStatusHistory
from apps.groups.models import ContactGroup

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')


def read_file_rows(file_path: str, file_type: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Reads CSV or XLSX and returns (headers, rows)."""
    headers = []
    rows = []

    if file_type.upper() == 'XLSX':
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheet = wb.active
        first = True
        for row_vals in sheet.iter_rows(values_only=True):
            if not row_vals or all(v is None for v in row_vals):
                continue
            if first:
                headers = [str(c).strip() if c is not None else f'Col_{i}' for i, c in enumerate(row_vals)]
                first = False
            else:
                row_dict = {}
                for i, col_name in enumerate(headers):
                    val = row_vals[i] if i < len(row_vals) else None
                    row_dict[col_name] = str(val).strip() if val is not None else ""
                rows.append(row_dict)
    else:
        # CSV reading with multiple encoding fallbacks
        encodings = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']
        content = None
        for enc in encodings:
            try:
                with open(file_path, 'r', encoding=enc) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue

        if content is None:
            raise ValueError("Could not decode CSV with supported encodings.")

        reader = csv.DictReader(io.StringIO(content))
        headers = [h.strip() for h in (reader.fieldnames or [])]
        for r in reader:
            rows.append({k.strip(): (v.strip() if v else "") for k, v in r.items() if k})

    return headers, rows


def analyze_import(rows: List[Dict[str, Any]], field_mapping: Dict[str, str]) -> Dict[str, Any]:
    """
    Performs pre-import analysis according to Section 11:
    Total Rows, Valid Contacts, New Contacts, Existing Contacts,
    Duplicate Contacts, Invalid Emails, Missing Email, Missing JobID.
    """
    email_field = field_mapping.get('email')
    job_id_field = field_mapping.get('job_id')

    total_rows = len(rows)
    valid_contacts = 0
    missing_emails = 0
    invalid_emails = 0
    missing_job_ids = 0
    duplicate_rows = 0

    seen_emails = set()
    existing_emails_in_db = set(Contact.objects.values_list('email', flat=True))

    new_count = 0
    existing_count = 0

    for r in rows:
        email = (r.get(email_field) or "").strip().lower() if email_field else ""
        job_id = (r.get(job_id_field) or "").strip() if job_id_field else ""

        is_row_valid = True

        if not email:
            missing_emails += 1
            is_row_valid = False
        elif not EMAIL_REGEX.match(email):
            invalid_emails += 1
            is_row_valid = False

        if not job_id:
            missing_job_ids += 1
            # JobID is optional for contacts; missing job_id does not invalidate contact row

        # In-file duplicate check
        if email and email in seen_emails:
            duplicate_rows += 1
            is_row_valid = False
        elif email:
            seen_emails.add(email)

        if is_row_valid:
            valid_contacts += 1
            if email in existing_emails_in_db:
                existing_count += 1
            else:
                new_count += 1

    return {
        'total_rows': total_rows,
        'valid_contacts': valid_contacts,
        'new_contacts': new_count,
        'existing_contacts': existing_count,
        'duplicate_rows': duplicate_rows,
        'invalid_emails': invalid_emails,
        'missing_emails': missing_emails,
        'missing_job_ids': missing_job_ids,
    }


def execute_import(
    rows: List[Dict[str, Any]],
    field_mapping: Dict[str, str],
    duplicate_strategy: str,
    target_group: ContactGroup = None,
    user=None
) -> Dict[str, int]:
    """Executes the mapped contact import with sensitive password encryption."""
    created_count = 0
    updated_count = 0
    skipped_count = 0

    name_field = field_mapping.get('name')
    first_name_field = field_mapping.get('first_name')
    last_name_field = field_mapping.get('last_name')
    email_field = field_mapping.get('email')
    phone_field = field_mapping.get('phone_number')
    password_field = field_mapping.get('login_password')
    job_id_field = field_mapping.get('job_id')
    status_field = field_mapping.get('status')

    contacts_to_add_to_group = []

    for r in rows:
        email = (r.get(email_field) or "").strip().lower() if email_field else ""
        job_id = (r.get(job_id_field) or "").strip() if job_id_field else ""

        if not email or not EMAIL_REGEX.match(email):
            skipped_count += 1
            continue

        raw_status = (r.get(status_field) or "UNUSED").strip().upper() if status_field else "UNUSED"
        status_val = "USED" if raw_status == "USED" else "UNUSED"

        name = (r.get(name_field) or "").strip() if name_field else ""
        first_name = (r.get(first_name_field) or "").strip() if first_name_field else ""
        last_name = (r.get(last_name_field) or "").strip() if last_name_field else ""
        phone = (r.get(phone_field) or "").strip() if phone_field else ""
        password = (r.get(password_field) or "").strip() if password_field else ""

        if not name:
            name = f"{first_name} {last_name}".strip() or email.split('@')[0]
        if not first_name and name:
            first_name = name.split(' ')[0]

        # Check existing by email (primary unique identifier)
        contact = Contact.objects.filter(email=email).first()

        if contact:
            if duplicate_strategy == 'SKIP':
                skipped_count += 1
                continue
            elif duplicate_strategy in ['UPDATE', 'ADD_TO_GROUP']:
                contact.name = name or contact.name
                contact.first_name = first_name or contact.first_name
                contact.last_name = last_name or contact.last_name
                contact.phone_number = phone or contact.phone_number
                contact.job_id = job_id or contact.job_id
                if password:
                    contact.login_password = password
                if status_field and contact.status != status_val:
                    old_s = contact.status
                    contact.status = status_val
                    ContactStatusHistory.objects.create(
                        contact=contact,
                        old_status=old_s,
                        new_status=status_val,
                        source='IMPORT',
                        changed_by=user,
                        notes="Status updated via file import"
                    )
                contact.save()
                updated_count += 1
                contacts_to_add_to_group.append(contact)
        else:
            contact = Contact(
                name=name,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone_number=phone,
                job_id=job_id,
                status=status_val,
                status_source='IMPORT'
            )
            if password:
                contact.login_password = password
            contact.save()
            created_count += 1
            contacts_to_add_to_group.append(contact)

    if target_group and contacts_to_add_to_group:
        target_group.contacts.add(*contacts_to_add_to_group)

    return {
        'created': created_count,
        'updated': updated_count,
        'skipped': skipped_count,
    }
