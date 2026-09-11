import time
import requests
import threading
from typing import List, Dict, Any, Tuple
from django.utils import timezone
from django.db import transaction
from .models import (
    ODKConnection, ODKProject, ODKForm, ODKFieldMapping,
    ODKSyncJob, ODKUnmatchedSubmission, ODKDataset, ODKEntity
)
from apps.contacts.models import Contact, ContactStatusHistory
from apps.sandbox.models import MockODKSubmission

_odk_sync_lock = threading.Lock()


class ODKClient:
    def __init__(self, connection: ODKConnection):
        self.connection = connection
        self.base_url = connection.base_url.rstrip('/')
        self._cached_token = None

    def get_token(self, force_refresh: bool = False) -> str:
        """
        Returns a valid Bearer token for ODK Central API requests.
        If connection has a username (email) and password, authenticates against /v1/sessions
        to obtain a short-lived session token.
        If no username is provided, treats password_or_token directly as an API/Bearer token.
        """
        if self._cached_token and not force_refresh:
            return self._cached_token

        username = (self.connection.username or "").strip()
        raw_secret = self.connection.password_or_token or ""

        # If no username is provided, assume raw_secret is a direct Bearer/API token
        if not username:
            self._cached_token = raw_secret
            return raw_secret

        # Authenticate via ODK Central /v1/sessions
        session_url = f"{self.base_url}/v1/sessions"
        resp = requests.post(
            session_url,
            json={"email": username, "password": raw_secret},
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get('token')
            if token:
                self._cached_token = token
                return token
            raise ValueError("ODK Central did not return a session token.")

        # If /v1/sessions failed, extract detailed error message
        try:
            err_data = resp.json()
            err_msg = err_data.get('message', resp.text)
        except Exception:
            err_msg = resp.text

        if resp.status_code == 401:
            raise ValueError(f"ODK Central login failed for '{username}' (HTTP 401): {err_msg}")
        raise ValueError(f"ODK Central session login returned HTTP {resp.status_code}: {err_msg}")

    def get_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        token = self.get_token(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Executes HTTP request, automatically refreshing session token once if 401 occurs."""
        headers = self.get_headers()
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))

        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401 and (self.connection.username or "").strip():
            # Refresh session token once and retry
            fresh_headers = self.get_headers(force_refresh=True)
            resp = requests.request(method, url, headers=fresh_headers, **kwargs)
        return resp

    def test_connection(self) -> Tuple[bool, str]:
        if self.connection.is_mock_sandbox:
            return True, "Mock ODK Central Sandbox is online and ready."
        try:
            # 1. Obtain token (authenticates via /v1/sessions if username is provided)
            self.get_token(force_refresh=True)

            # 2. Verify access to projects
            url = f"{self.base_url}/v1/projects"
            resp = self._request_with_retry('GET', url, timeout=15)
            if resp.status_code == 200:
                projects = resp.json()
                count = len(projects) if isinstance(projects, list) else 0
                return True, f"ODK Central verified successfully! Found {count} accessible project{'s' if count != 1 else ''}."
            return False, f"ODK Central responded with status {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    def discover_projects(self) -> List[Dict[str, Any]]:
        if self.connection.is_mock_sandbox:
            return [{"id": 1, "name": "Pulse Survey 2026", "description": "Research Pulse Survey Sandbox"}]
        url = f"{self.base_url}/v1/projects"
        resp = self._request_with_retry('GET', url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def discover_forms(self, project_id: int) -> List[Dict[str, Any]]:
        if self.connection.is_mock_sandbox:
            return [{"xmlFormId": "pulse_v1", "name": "Pulse_V1", "version": "1.0", "submissions": MockODKSubmission.objects.count()}]
        url = f"{self.base_url}/v1/projects/{project_id}/forms"
        resp = self._request_with_retry('GET', url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch_submissions(self, project_id: int, xml_form_id: str, last_sync_timestamp=None) -> List[Dict[str, Any]]:
        if self.connection.is_mock_sandbox:
            qs = MockODKSubmission.objects.filter(project_id=str(project_id), form_id=xml_form_id)
            if last_sync_timestamp:
                qs = qs.filter(submitted_at__gt=last_sync_timestamp)
            results = []
            for sub in qs:
                results.append({
                    "instanceId": sub.submission_id,
                    "submittedAt": sub.submitted_at.isoformat(),
                    "data": {
                        "job_id": sub.job_id,
                        "email": sub.respondent_email,
                        **sub.data
                    }
                })
            return results

        # Real ODK Central API
        url = f"{self.base_url}/v1/projects/{project_id}/forms/{xml_form_id}/submissions.csv"
        headers = self.get_headers()
        headers["Accept"] = "text/csv"
        resp = self._request_with_retry('GET', url, headers=headers, timeout=30)
        resp.raise_for_status()
        # Parse CSV submissions from ODK Central
        import csv
        import io
        reader = csv.DictReader(io.StringIO(resp.text))
        submissions = []
        for r in reader:
            sub_id = r.get('instanceID') or r.get('__id') or r.get('meta-instanceID') or r.get('meta:instanceID') or r.get('KEY')
            submitted_at = r.get('SubmissionDate') or r.get('submission_date') or timezone.now().isoformat()
            submissions.append({
                "instanceId": sub_id,
                "submittedAt": submitted_at,
                "data": r
            })
        return submissions

    def discover_datasets(self, project_id: int) -> List[Dict[str, Any]]:
        """Discovers entity lists (datasets) configured in a project."""
        if self.connection.is_mock_sandbox:
            return [
                {
                    "name": "pulse_respondents_2026",
                    "description": "Registered Survey Respondents Cohort",
                    "entities": 4,
                    "lastEntity": timezone.now().isoformat()
                },
                {
                    "name": "facility_roster",
                    "description": "Participating Healthcare Facilities",
                    "entities": 1,
                    "lastEntity": timezone.now().isoformat()
                }
            ]
        url = f"{self.base_url}/v1/projects/{project_id}/datasets"
        resp = self._request_with_retry('GET', url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return []

    def fetch_entities(self, project_id: int, dataset_name: str) -> List[Dict[str, Any]]:
        """Fetches entities from an entity list (dataset)."""
        if self.connection.is_mock_sandbox:
            if dataset_name == "pulse_respondents_2026":
                return [
                    {
                        "uuid": "ent-001",
                        "label": "Ali Khan",
                        "currentVersion": {
                            "version": 1,
                            "data": {
                                "first_name": "Ali",
                                "last_name": "Khan",
                                "email": "ali.khan@example.com",
                                "job_id": "JOB-1025",
                                "phone": "03001234567"
                            }
                        },
                        "createdAt": timezone.now().isoformat()
                    },
                    {
                        "uuid": "ent-002",
                        "label": "Sara Ahmed",
                        "currentVersion": {
                            "version": 1,
                            "data": {
                                "first_name": "Sara",
                                "last_name": "Ahmed",
                                "email": "sara.ahmed@example.com",
                                "job_id": "JOB-1026",
                                "phone": "03007654321"
                            }
                        },
                        "createdAt": timezone.now().isoformat()
                    },
                    {
                        "uuid": "ent-003",
                        "label": "Usman Tariq",
                        "currentVersion": {
                            "version": 1,
                            "data": {
                                "first_name": "Usman",
                                "last_name": "Tariq",
                                "email": "usman.tariq@example.com",
                                "job_id": "JOB-1027",
                                "phone": "03009988776"
                            }
                        },
                        "createdAt": timezone.now().isoformat()
                    },
                    {
                        "uuid": "ent-004",
                        "label": "Fatima Noor",
                        "currentVersion": {
                            "version": 1,
                            "data": {
                                "first_name": "Fatima",
                                "last_name": "Noor",
                                "email": "fatima.noor@example.com",
                                "job_id": "JOB-1028",
                                "phone": "03005544332"
                            }
                        },
                        "createdAt": timezone.now().isoformat()
                    }
                ]
            return [
                {
                    "uuid": "ent-facility-1",
                    "label": "Central Hospital Lahore",
                    "currentVersion": {
                        "version": 1,
                        "data": {"city": "Lahore", "type": "Hospital"}
                    },
                    "createdAt": timezone.now().isoformat()
                }
            ]

        # Real ODK Central API: Prefer CSV endpoint as it returns all custom columns and properties
        csv_url = f"{self.base_url}/v1/projects/{project_id}/datasets/{dataset_name}/entities.csv"
        csv_headers = self.get_headers()
        csv_headers["Accept"] = "text/csv"
        try:
            csv_resp = self._request_with_retry('GET', csv_url, headers=csv_headers, timeout=30)
            if csv_resp.status_code == 200:
                import csv
                import io
                reader = csv.DictReader(io.StringIO(csv_resp.text))
                entities = []
                for r in reader:
                    uuid_val = r.get('__id') or r.get('uuid') or r.get('meta:instanceID') or f"ent-{len(entities)+1}"
                    label_val = r.get('label') or r.get('name') or uuid_val
                    # Filter out internal double-underscore keys for clean property data
                    prop_data = {k: v for k, v in r.items() if not k.startswith('__') and v != ''}
                    entities.append({
                        "uuid": uuid_val,
                        "label": label_val,
                        "currentVersion": {
                            "version": int(r.get('__version') or 1),
                            "data": prop_data
                        },
                        "createdAt": r.get('__system:createdAt') or r.get('__createdAt') or timezone.now().isoformat()
                    })
                return entities
        except Exception:
            pass

        # Fallback to JSON entities endpoint
        url = f"{self.base_url}/v1/projects/{project_id}/datasets/{dataset_name}/entities"
        resp = self._request_with_retry('GET', url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        resp.raise_for_status()
        return []



def extract_entity_properties(data: dict, label: str = '', uuid_val: str = '') -> dict:
    """Extracts email, name, phone, job_id, status from entity properties/data."""
    # Email
    email = ''
    for k in ['respondent_email', 'email', 'Email', 'Respondent_Email', 'email_address', 'mail']:
        val = str(data.get(k) or '').strip().lower()
        if val and '@' in val:
            email = val
            break
    if not email and label and '@' in label:
        email = label.strip().lower()

    # Name
    fn = str(data.get('first_name') or '').strip()
    ln = str(data.get('last_name') or '').strip()
    name = ''
    for k in ['p_name', 'name', 'respondent_name', 'full_name', 'contact_name', 'Name']:
        val = str(data.get(k) or '').strip()
        if val:
            name = val
            break
    if not name:
        if fn or ln:
            name = f"{fn} {ln}".strip()
        elif label and not ('@' in label):
            name = label.strip()

    # Phone
    phone = ''
    for k in ['login', 'phone', 'mobile', 'phone_number', 'contact_no', 'token']:
        val = str(data.get(k) or '').strip()
        if val and any(ch.isdigit() for ch in val):
            phone = val
            break

    # Job ID / Identifier
    job_id = uuid_val or ''
    for k in ['job_id', 'jobId', 'JobID', 'token_entity_id', 'token']:
        val = str(data.get(k) or '').strip()
        if val:
            job_id = val
            break

    # Status
    raw_status = str(data.get('status') or data.get('Status') or '').strip().upper()
    if 'USED' in raw_status and 'UNUSED' not in raw_status:
        status_val = 'USED'
    elif 'UNUSED' in raw_status:
        status_val = 'UNUSED'
    elif 'used' in data:
        val = str(data.get('used')).strip().lower()
        status_val = 'USED' if val in ['1', 'true', 'yes', 'y'] else 'UNUSED'
    elif data.get('used_at'):
        status_val = 'USED'
    else:
        status_val = 'UNUSED'

    return {
        'email': email,
        'name': name,
        'first_name': fn,
        'last_name': ln,
        'phone': phone,
        'job_id': job_id,
        'status': status_val
    }


def sync_dataset_entities(dataset: ODKDataset) -> int:
    """Fetches entities from ODK Central and updates the local ODKEntity database cache."""
    client = ODKClient(dataset.project.connection)
    entities_data = client.fetch_entities(dataset.project.odk_id, dataset.name)
    count = 0
    unused_c = 0
    used_c = 0
    for e in entities_data:
        uuid_val = e.get('uuid') or e.get('__id')
        if not uuid_val:
            continue
        label = e.get('label') or ''
        curr_ver = e.get('currentVersion') or {}
        ver = curr_ver.get('version', 1)
        data = curr_ver.get('data') or (e if not curr_ver else {})

        props = extract_entity_properties(data, label, uuid_val)
        status_val = props['status']

        if status_val == 'USED':
            used_c += 1
        else:
            unused_c += 1

        ODKEntity.objects.update_or_create(
            dataset=dataset,
            uuid=uuid_val,
            defaults={
                'label': label,
                'version': ver,
                'status': status_val,
                'data': data
            }
        )

        # Match and update contact status
        try:
            from apps.contacts.models import Contact
            contact = None
            if uuid_val:
                contact = Contact.objects.filter(job_id=uuid_val).first()
            if not contact and props['job_id']:
                contact = Contact.objects.filter(job_id=props['job_id']).first()
            if not contact and props['phone']:
                contact = Contact.objects.filter(phone_number=props['phone']).first()
            if not contact and props['email']:
                contact = Contact.objects.filter(email__iexact=props['email']).first()

            if contact:
                contact.odk_last_checked_at = timezone.now()
                update_fields = ['odk_last_checked_at']
                if status_val == 'USED' and contact.status != Contact.UsageStatus.USED:
                    old_status = contact.status
                    contact.status = Contact.UsageStatus.USED
                    contact.status_source = 'ODK_CENTRAL'
                    update_fields.extend(['status', 'status_source'])
                    if data.get('used_at'):
                        try:
                            from django.utils.dateparse import parse_datetime
                            dt = parse_datetime(str(data['used_at']))
                            if dt:
                                contact.odk_submitted_at = dt
                                update_fields.append('odk_submitted_at')
                        except Exception:
                            pass
                    if data.get('submission_uuid'):
                        contact.odk_submission_id = str(data['submission_uuid']).strip()
                        update_fields.append('odk_submission_id')
                    contact.save(update_fields=update_fields)
                    ContactStatusHistory.objects.create(
                        contact=contact,
                        old_status=old_status,
                        new_status=Contact.UsageStatus.USED,
                        source='ODK_CENTRAL',
                        notes=f"ODK Dataset '{dataset.name}' updated status to USED"
                    )
                else:
                    contact.save(update_fields=['odk_last_checked_at'])
        except Exception:
            pass

        count += 1
    dataset.entities_count = count
    dataset.unused_count = unused_c
    dataset.used_count = used_c
    dataset.last_entity_at = timezone.now()
    dataset.save(update_fields=['entities_count', 'unused_count', 'used_count', 'last_entity_at'])
    return count




def run_odk_sync(form: ODKForm, trigger_source: str = 'MANUAL', user=None) -> ODKSyncJob:
    """
    Executes ODK submission synchronization for a given form.
    Extracts JobIDs, matches contacts, updates status to USED, and records unmatched records.
    (Section 19, 20, 22)
    """
    start_time = time.time()
    connection = form.project.connection

    sync_job = ODKSyncJob.objects.create(
        connection=connection,
        form=form,
        status=ODKSyncJob.Status.RUNNING,
        trigger_source=trigger_source,
        triggered_by=user
    )

    try:
        mapping, _ = ODKFieldMapping.objects.get_or_create(form=form)
        job_id_field = mapping.job_id_field or 'job_id'

        client = ODKClient(connection)
        # Fetch submissions (incremental if timestamp exists)
        submissions = client.fetch_submissions(
            project_id=form.project.odk_id,
            xml_form_id=form.odk_xml_form_id,
            last_sync_timestamp=form.last_submission_timestamp
        )

        submissions_checked = len(submissions)
        new_submissions = submissions_checked
        contacts_marked_used = 0
        unmatched_count = 0

        latest_timestamp = form.last_submission_timestamp
        latest_sub_id = form.last_submission_id

        unmatched_to_create = []

        with transaction.atomic():
            for sub in submissions:
                sub_id = sub.get('instanceId') or ''
                raw_data = sub.get('data') or {}
                job_id_val = str(raw_data.get(job_id_field) or '').strip()
                if not job_id_val:
                    for candidate in ['token_entity_id', 'job_id', 'jobId', 'JobID', 'token', 'entered_login', 'login', 'farmer_code', 'respondent_id']:
                        for k, v in raw_data.items():
                            if candidate in k.lower() and v:
                                job_id_val = str(v).strip()
                                break
                        if job_id_val:
                            break

                contact = None
                if job_id_val:
                    contact = Contact.objects.filter(job_id=job_id_val).first()
                    if not contact:
                        contact = Contact.objects.filter(phone_number=job_id_val).first()
                    if not contact and '@' in job_id_val:
                        contact = Contact.objects.filter(email__iexact=job_id_val).first()

                if not contact:
                    for k, v in raw_data.items():
                        if 'email' in k.lower() and v and '@' in str(v):
                            contact = Contact.objects.filter(email__iexact=str(v).strip()).first()
                            if contact:
                                break

                if not contact:
                    unmatched_count += 1
                    unmatched_to_create.append(ODKUnmatchedSubmission(
                        sync_job=sync_job,
                        submission_id=sub_id,
                        job_id_value=job_id_val or "",
                        reason="Contact not found by JobID/Phone/Email" if job_id_val else "JobID empty in submission",
                        raw_data=raw_data
                    ))
                    continue

                # Contact matched! Check if status transition is needed
                if contact.status != Contact.UsageStatus.USED:
                    old_status = contact.status
                    contact.status = Contact.UsageStatus.USED
                    contact.odk_submission_id = sub_id
                    contact.odk_submitted_at = timezone.now()
                    contact.odk_last_checked_at = timezone.now()
                    contact.status_source = 'ODK_CENTRAL'
                    contact.save(update_fields=[
                        'status', 'odk_submission_id', 'odk_submitted_at',
                        'odk_last_checked_at', 'status_source'
                    ])

                    ContactStatusHistory.objects.create(
                        contact=contact,
                        old_status=old_status,
                        new_status=Contact.UsageStatus.USED,
                        source='ODK_CENTRAL',
                        odk_submission_id=sub_id,
                        changed_by=user,
                        notes=f"ODK submission detected on form {form.name}"
                    )
                    contacts_marked_used += 1
                else:
                    # Update check time
                    contact.odk_last_checked_at = timezone.now()
                    contact.save(update_fields=['odk_last_checked_at'])

            if unmatched_to_create:
                ODKUnmatchedSubmission.objects.bulk_create(unmatched_to_create)

        duration = round(time.time() - start_time, 2)
        now = timezone.now()

        # Update Form stats
        form.last_sync_at = now
        form.last_submission_timestamp = now
        form.submissions_count += contacts_marked_used
        form.save(update_fields=['last_sync_at', 'last_submission_timestamp', 'submissions_count'])

        sync_job.status = ODKSyncJob.Status.COMPLETED
        sync_job.submissions_checked = submissions_checked
        sync_job.new_submissions = new_submissions
        sync_job.contacts_marked_used = contacts_marked_used
        sync_job.unmatched_job_ids = unmatched_count
        sync_job.duration_seconds = duration
        sync_job.completed_at = now
        sync_job.save()

        return sync_job

    except Exception as e:
        duration = round(time.time() - start_time, 2)
        sync_job.status = ODKSyncJob.Status.FAILED
        sync_job.error_message = str(e)
        sync_job.duration_seconds = duration
        sync_job.completed_at = timezone.now()
        sync_job.save()
        raise e


def sync_all_odk_contacts(trigger_source: str = 'MANUAL', user=None, auto_import: bool = False) -> Dict[str, Any]:
    """
    Unified, high-performance sync engine for ODK Central:
    1. Acquires a thread lock to prevent SQLite lockups and concurrent sync collisions.
    2. Synchronizes active datasets (Entity Lists) first, updating matching contacts to USED.
    3. Synchronizes forms ONLY if linked to active campaigns, avoiding massive unneeded HTTP crawls.
    4. Returns clean status metrics and timestamp.
    """
    from django.db.models import Q
    from django.utils.dateparse import parse_datetime
    from apps.campaigns.models import Campaign

    # Concurrency guard: avoid database lockups
    if not _odk_sync_lock.acquire(blocking=False):
        return {
            'status': 'in_progress',
            'message': 'ODK Central synchronization is already running in the background. Please wait a moment.',
            'forms_synced': 0,
            'datasets_synced': 0,
            'submissions_checked': 0,
            'contacts_marked_used': 0,
            'entities_synced': 0,
            'new_contacts_imported': 0,
            'errors': []
        }

    try:
        active_connections = ODKConnection.objects.filter(is_active=True)
        if not active_connections.exists():
            return {
                'status': 'warning',
                'message': 'No active ODK Central connection found.',
                'forms_synced': 0,
                'datasets_synced': 0,
                'submissions_checked': 0,
                'contacts_marked_used': 0,
                'entities_synced': 0,
                'new_contacts_imported': 0,
                'errors': []
            }

        total_forms_synced = 0
        total_submissions_checked = 0
        total_contacts_marked_used = 0
        total_datasets_synced = 0
        total_entities_synced = 0
        total_new_contacts = 0
        errors = []

        # Find which datasets and forms are linked to campaigns
        campaign_dataset_ids = list(Campaign.objects.filter(odk_dataset__isnull=False).values_list('odk_dataset_id', flat=True))
        campaign_form_ids = list(Campaign.objects.filter(odk_form__isnull=False).values_list('odk_form_id', flat=True))

        for connection in active_connections:
            # 1. Sync Datasets (Entity Lists) FIRST - Fast & matches user campaigns
            if campaign_dataset_ids:
                datasets_to_sync = ODKDataset.objects.filter(
                    project__connection=connection,
                    id__in=campaign_dataset_ids
                ).distinct()
            else:
                datasets_to_sync = ODKDataset.objects.filter(
                    project__connection=connection
                ).filter(
                    Q(entities_count__gt=0) |
                    Q(last_entity_at__isnull=False)
                ).distinct()

            if not datasets_to_sync.exists():
                datasets_to_sync = ODKDataset.objects.filter(project__connection=connection)[:3]

            for dataset in datasets_to_sync:
                try:
                    ent_count = sync_dataset_entities(dataset)
                    total_datasets_synced += 1
                    total_entities_synced += ent_count

                    # Check dataset entities against contacts
                    entities = dataset.entities.all()
                    for ent in entities:
                        uuid_val = ent.uuid or ''
                        label = ent.label or ''
                        data = ent.data or {}
                        props = extract_entity_properties(data, label, uuid_val)
                        ent_status = props['status']

                        contact = None
                        if uuid_val:
                            contact = Contact.objects.filter(job_id=uuid_val).first()
                        if not contact and props['job_id']:
                            contact = Contact.objects.filter(job_id=props['job_id']).first()
                        if not contact and props['phone']:
                            contact = Contact.objects.filter(phone_number=props['phone']).first()
                        if not contact and props['email']:
                            contact = Contact.objects.filter(email__iexact=props['email']).first()

                        if contact:
                            if ent_status == 'USED' and contact.status != Contact.UsageStatus.USED:
                                old_status = contact.status
                                contact.status = Contact.UsageStatus.USED
                                contact.status_source = 'ODK_CENTRAL'
                                contact.odk_last_checked_at = timezone.now()
                                if data.get('used_at'):
                                    try:
                                        dt = parse_datetime(str(data['used_at']))
                                        if dt:
                                            contact.odk_submitted_at = dt
                                    except Exception:
                                        pass
                                if data.get('submission_uuid'):
                                    contact.odk_submission_id = str(data['submission_uuid']).strip()
                                contact.save()
                                ContactStatusHistory.objects.create(
                                    contact=contact,
                                    old_status=old_status,
                                    new_status=Contact.UsageStatus.USED,
                                    source='ODK_CENTRAL',
                                    changed_by=user,
                                    notes=f"ODK Dataset '{dataset.name}' updated status to USED"
                                )
                                total_contacts_marked_used += 1
                            else:
                                contact.odk_last_checked_at = timezone.now()
                                contact.save(update_fields=['odk_last_checked_at'])
                        elif auto_import and props['email']:
                            Contact.objects.create(
                                email=props['email'],
                                name=props['name'],
                                first_name=props['first_name'],
                                last_name=props['last_name'],
                                phone_number=props['phone'],
                                job_id=uuid_val or props['job_id'],
                                status=Contact.UsageStatus.USED if ent_status == 'USED' else Contact.UsageStatus.UNUSED,
                                status_source='ODK_CENTRAL',
                                odk_last_checked_at=timezone.now()
                            )
                            total_new_contacts += 1

                except Exception as e:
                    errors.append(f"Dataset '{dataset.name}': {str(e)}")

            # 2. Sync Forms - ONLY forms linked to active campaigns to prevent crawling hundreds of unrelated forms
            if campaign_form_ids:
                forms_to_sync = ODKForm.objects.filter(
                    project__connection=connection,
                    id__in=campaign_form_ids
                ).distinct()
            else:
                forms_to_sync = ODKForm.objects.none()

            for form in forms_to_sync:
                try:
                    job = run_odk_sync(form, trigger_source=trigger_source, user=user)
                    total_forms_synced += 1
                    total_submissions_checked += job.submissions_checked
                    total_contacts_marked_used += job.contacts_marked_used
                except Exception as e:
                    errors.append(f"Form '{form.name}': {str(e)}")

        summary_msg = f"Synced {total_datasets_synced} dataset(s) and {total_forms_synced} form(s). Updated {total_contacts_marked_used} contact(s)."
        if total_new_contacts > 0:
            summary_msg += f" Imported {total_new_contacts} new contact(s)."

        return {
            'status': 'success' if (total_forms_synced > 0 or total_datasets_synced > 0) else ('error' if errors else 'warning'),
            'forms_synced': total_forms_synced,
            'datasets_synced': total_datasets_synced,
            'submissions_checked': total_submissions_checked,
            'contacts_marked_used': total_contacts_marked_used,
            'entities_synced': total_entities_synced,
            'new_contacts_imported': total_new_contacts,
            'errors': errors,
            'message': summary_msg,
            'timestamp': timezone.now().isoformat()
        }
    finally:
        _odk_sync_lock.release()
