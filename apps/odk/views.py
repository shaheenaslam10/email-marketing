from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import (
    ODKConnection, ODKProject, ODKForm, ODKFieldMapping,
    ODKSyncJob, ODKUnmatchedSubmission, ODKDataset, ODKEntity
)
from .serializers import (
    ODKConnectionSerializer, ODKProjectSerializer, ODKFormSerializer,
    ODKFieldMappingSerializer, ODKSyncJobSerializer, ODKUnmatchedSubmissionSerializer,
    ODKDatasetSerializer, ODKEntitySerializer
)
from .services import ODKClient, run_odk_sync, sync_dataset_entities
from email.utils import parseaddr
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup



class ODKConnectionViewSet(viewsets.ModelViewSet):
    queryset = ODKConnection.objects.all().order_by('-created_at')
    serializer_class = ODKConnectionSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        conn = self.get_object()
        client = ODKClient(conn)
        success, msg = client.test_connection()
        conn.status = ODKConnection.Status.CONNECTED if success else ODKConnection.Status.FAILED
        conn.last_connection_test = timezone.now()
        conn.last_test_message = msg
        conn.save(update_fields=['status', 'last_connection_test', 'last_test_message'])
        return Response({'success': success, 'message': msg})

    @action(detail=True, methods=['post'])
    def discover(self, request, pk=None):
        """Discovers projects, forms, and entity lists (datasets) from ODK Central and updates the database."""
        conn = self.get_object()
        client = ODKClient(conn)
        try:
            projects_data = client.discover_projects()
            discovered = []
            for p_data in projects_data:
                proj, _ = ODKProject.objects.update_or_create(
                    connection=conn,
                    odk_id=p_data['id'],
                    defaults={
                        'name': p_data.get('name') or f"Project {p_data['id']}",
                        'description': p_data.get('description') or ''
                    }
                )
                forms_data = client.discover_forms(proj.odk_id)
                for f_data in forms_data:
                    form_obj, _ = ODKForm.objects.update_or_create(
                        project=proj,
                        odk_xml_form_id=f_data['xmlFormId'],
                        defaults={
                            'name': f_data.get('name') or f_data['xmlFormId'],
                            'version': f_data.get('version') or '',
                        }
                    )
                    # Create default mapping if not exists
                    ODKFieldMapping.objects.get_or_create(form=form_obj)

                # Discover Entity Lists (Datasets)
                try:
                    datasets_data = client.discover_datasets(proj.odk_id)
                    for d_data in datasets_data:
                        d_obj, _ = ODKDataset.objects.update_or_create(
                            project=proj,
                            name=d_data['name'],
                            defaults={
                                'description': d_data.get('description') or '',
                                'entities_count': d_data.get('entities') or 0,
                            }
                        )
                        # Pre-sync entities if mock sandbox
                        if conn.is_mock_sandbox and d_obj.entities.count() == 0:
                            sync_dataset_entities(d_obj)
                except Exception as dataset_err:
                    datasets_data = []

                discovered.append({
                    'id': proj.id,
                    'name': proj.name,
                    'forms_count': len(forms_data),
                    'datasets_count': len(datasets_data)
                })

            return Response({'status': 'success', 'projects': discovered})
        except Exception as e:
            return Response({'error': f"Discovery failed: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)


class ODKFormViewSet(viewsets.ModelViewSet):
    queryset = ODKForm.objects.all().order_by('-created_at')
    serializer_class = ODKFormSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Runs immediate synchronization on this form and returns Brevo-style summary."""
        form = self.get_object()
        try:
            sync_job = run_odk_sync(form=form, trigger_source='MANUAL', user=request.user)
            return Response({
                'status': 'completed',
                'sync_job_id': sync_job.id,
                'submissions_checked': sync_job.submissions_checked,
                'new_submissions': sync_job.new_submissions,
                'contacts_marked_used': sync_job.contacts_marked_used,
                'unmatched_job_ids': sync_job.unmatched_job_ids,
                'duration_seconds': sync_job.duration_seconds,
            })
        except Exception as e:
            return Response({'error': f"Sync failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='field-mapping')
    def map_fields(self, request, pk=None):
        form = self.get_object()
        mapping, _ = ODKFieldMapping.objects.get_or_create(form=form)
        job_id_field = request.data.get('job_id_field', 'job_id')
        email_field = request.data.get('email_field', '')
        phone_field = request.data.get('phone_field', '')

        mapping.job_id_field = job_id_field.strip()
        mapping.email_field = email_field.strip()
        mapping.phone_field = phone_field.strip()
        mapping.save()

        return Response(ODKFieldMappingSerializer(mapping).data)


class ODKDatasetViewSet(viewsets.ModelViewSet):
    """Viewset for ODK Central Entity Lists (Datasets)."""
    queryset = ODKDataset.objects.all().order_by('-created_at')
    serializer_class = ODKDatasetSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=['get'])
    def entities(self, request, pk=None):
        """Returns paginated entities of the dataset."""
        dataset = self.get_object()
        # If no entities cached yet, try initial sync
        if dataset.entities.count() == 0:
            try:
                sync_dataset_entities(dataset)
            except Exception:
                pass
        entities_qs = dataset.entities.all().order_by('-created_at')
        page = self.paginate_queryset(entities_qs)
        if page is not None:
            serializer = ODKEntitySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = ODKEntitySerializer(entities_qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Fetches latest entities from ODK Central and updates database cache."""
        dataset = self.get_object()
        try:
            count = sync_dataset_entities(dataset)
            return Response({
                'status': 'success',
                'dataset_id': dataset.id,
                'name': dataset.name,
                'entities_count': count,
                'message': f"Successfully synced {count} entities from ODK Central"
            })
        except Exception as e:
            return Response({'error': f"Failed to sync entities: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def fields(self, request, pk=None):
        """Returns available attributes/keys found in the dataset's entities for field mapping."""
        dataset = self.get_object()
        if dataset.entities.count() == 0:
            try:
                sync_dataset_entities(dataset)
            except Exception:
                pass

        fields_set = set(['label', 'uuid'])
        sample_values = {}
        sample_entities = dataset.entities.all()[:20]
        for ent in sample_entities:
            if ent.label and 'label' not in sample_values:
                sample_values['label'] = ent.label
            if ent.uuid and 'uuid' not in sample_values:
                sample_values['uuid'] = ent.uuid
            if ent.data:
                for k, v in ent.data.items():
                    if not k.startswith('__'):
                        fields_set.add(k)
                        if k not in sample_values and v:
                            sample_values[k] = str(v)[:50]

        return Response({
            'dataset_id': dataset.id,
            'name': dataset.name,
            'fields': sorted(list(fields_set)),
            'samples': sample_values
        })

    @action(detail=True, methods=['post'], url_path='import-to-contacts')
    def import_to_contacts(self, request, pk=None):
        """Imports entities from this dataset into Contact records, optionally attaching them to a ContactGroup."""
        dataset = self.get_object()
        group_id = request.data.get('group_id')
        group_name = request.data.get('group_name')
        mapping = request.data.get('mapping') or {}

        map_email = mapping.get('email')
        map_name = mapping.get('name')
        map_first_name = mapping.get('first_name')
        map_last_name = mapping.get('last_name')
        map_job_id = mapping.get('job_id')
        map_phone = mapping.get('phone')
        map_password = mapping.get('password')
        map_status = mapping.get('status')

        group = None
        if group_id:
            group = ContactGroup.objects.filter(id=group_id).first()
        elif group_name and str(group_name).strip():
            group, _ = ContactGroup.objects.get_or_create(name=str(group_name).strip())

        CORE_CONTACT_FIELDS = {
            'email', 'name', 'first_name', 'last_name', 'job_id',
            'phone', 'phone_number', 'password', 'login_password', 'status'
        }
        mapped_odk_cols = set()
        custom_fields_by_key = {}

        # Store selected fields and mapping schema on the group
        if group and mapping:
            selected_keys = []
            for k in ['name', 'first_name', 'last_name', 'email', 'job_id', 'phone', 'password', 'status']:
                if mapping.get(k):
                    norm_k = 'phone_number' if k == 'phone' else ('login_password' if k == 'password' else k)
                    if norm_k not in selected_keys:
                        selected_keys.append(norm_k)
            if ('first_name' in selected_keys or 'last_name' in selected_keys) and 'name' not in selected_keys:
                selected_keys.insert(0, 'name')
            if 'email' not in selected_keys:
                selected_keys.insert(0, 'email')

            group.selected_fields = selected_keys
            group.field_mappings = mapping
            group.save(update_fields=['selected_fields', 'field_mappings'])

            # ── Auto-create GroupCustomField for every ODK field in the mapping ──────
            from apps.groups.models import GroupCustomField, GroupContactValue
            from django.utils.text import slugify

            for k, v in mapping.items():
                if v and k.strip().lower() in CORE_CONTACT_FIELDS:
                    mapped_odk_cols.add(str(v).strip())

            for field_key, odk_column in mapping.items():
                if not odk_column:
                    continue
                norm_key = field_key.strip().lower()
                if norm_key in CORE_CONTACT_FIELDS:
                    continue
                col_name = str(odk_column).strip()
                slug = slugify(col_name) or norm_key
                gcf, _ = GroupCustomField.objects.get_or_create(
                    group=group,
                    slug=slug,
                    defaults={
                        'name': col_name.replace('_', ' ').title(),
                        'field_type': GroupCustomField.FieldType.TEXT,
                        'description': f'Imported from ODK field: {col_name}',
                        'is_active': True,
                    }
                )
                custom_fields_by_key[col_name] = gcf
        elif group:
            from apps.groups.models import GroupCustomField, GroupContactValue
            from django.utils.text import slugify

        # Ensure entities are fetched
        if dataset.entities.count() == 0:
            try:
                sync_dataset_entities(dataset)
            except Exception as e:
                return Response({'error': f"Failed to sync entities before import: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        entities = dataset.entities.all()
        created_count = 0
        updated_count = 0
        skipped_count = 0

        # Auto-discover all non-core entity attributes across entities and create GroupCustomField
        if group:
            discovered_attrs = set()
            for ent in entities[:50]:
                if ent.data:
                    for attr in ent.data.keys():
                        if not attr.startswith('__') and attr not in mapped_odk_cols and attr.lower() not in CORE_CONTACT_FIELDS:
                            discovered_attrs.add(attr)
            for attr in sorted(discovered_attrs):
                slug = slugify(attr)
                if not slug:
                    continue
                gcf, _ = GroupCustomField.objects.get_or_create(
                    group=group,
                    slug=slug,
                    defaults={
                        'name': attr.replace('_', ' ').title(),
                        'field_type': GroupCustomField.FieldType.TEXT,
                        'description': f'Imported from ODK attribute: {attr}',
                        'is_active': True,
                    }
                )
                custom_fields_by_key[attr] = gcf

        for ent in entities:
            data = ent.data or {}

            # 1. Resolve Email
            if map_email:
                raw_email = getattr(ent, map_email, None) if map_email in ['label', 'uuid'] else data.get(map_email)
            else:
                raw_email = (
                    data.get('email') or data.get('Email') or data.get('respondent_email') or
                    data.get('email_address') or data.get('mail') or ''
                )
                if not raw_email and '@' in ent.label:
                    raw_email = ent.label

            raw_email = str(raw_email or '').strip()
            parsed_name, parsed_email = parseaddr(raw_email)
            email = (parsed_email or raw_email).strip().lower()

            if not email or '@' not in email:
                skipped_count += 1
                continue

            # 2. Resolve Name
            if map_name:
                name_val = getattr(ent, map_name, None) if map_name in ['label', 'uuid'] else data.get(map_name)
                name = str(name_val or '').strip()
            else:
                respondent_name = (
                    data.get('respondent_name') or data.get('e_name') or data.get('p_name') or
                    data.get('farmer_name') or data.get('name') or ''
                ).strip()
                name = (respondent_name or parsed_name or ent.label or '').strip()

            # 3. Resolve First Name & Last Name
            if map_first_name:
                fn_val = getattr(ent, map_first_name, None) if map_first_name in ['label', 'uuid'] else data.get(map_first_name)
                first_name = str(fn_val or '').strip()
            else:
                first_name = (data.get('first_name') or data.get('firstName') or data.get('fname') or '').strip()
                if not first_name and name:
                    first_name = name.split()[0]

            if map_last_name:
                ln_val = getattr(ent, map_last_name, None) if map_last_name in ['label', 'uuid'] else data.get(map_last_name)
                last_name = str(ln_val or '').strip()
            else:
                last_name = (data.get('last_name') or data.get('lastName') or data.get('lname') or '').strip()
                if not last_name and name and len(name.split()) > 1:
                    last_name = ' '.join(name.split()[1:])

            if not name:
                if first_name or last_name:
                    name = f"{first_name} {last_name}".strip()
                else:
                    name = parsed_name or ent.label or email.split('@')[0]

            # 4. Resolve Phone
            if map_phone:
                phone_val = getattr(ent, map_phone, None) if map_phone in ['label', 'uuid'] else data.get(map_phone)
                phone = str(phone_val or '').strip()
            else:
                raw_phone = data.get('phone') or data.get('phone_number') or data.get('phoneNumber') or data.get('mobile') or ''
                if not raw_phone and data.get('token') and str(data.get('token')).isdigit() and len(str(data.get('token'))) >= 10:
                    raw_phone = str(data.get('token'))
                phone = str(raw_phone).strip()

            # 5. Resolve Job ID / Token
            if map_job_id:
                job_id_val = getattr(ent, map_job_id, None) if map_job_id in ['label', 'uuid'] else data.get(map_job_id)
                job_id = str(job_id_val or '').strip()
            else:
                job_id = (data.get('job_id') or data.get('jobId') or data.get('JobID') or data.get('token') or data.get('farmer_code') or ent.uuid or '').strip()

            # 6. Resolve Password
            if map_password:
                pass_val = getattr(ent, map_password, None) if map_password in ['label', 'uuid'] else data.get(map_password)
                password = str(pass_val or '').strip()
            else:
                password = (data.get('password') or data.get('login') or '').strip()

            # 7. Resolve Status (Auto-Import from Entity)
            if map_status:
                raw_status = str(getattr(ent, map_status, None) if map_status in ['label', 'uuid'] else data.get(map_status) or '').strip().upper()
            else:
                raw_status = str(data.get('status') or data.get('Status') or ent.status or '').strip().upper()

            if 'USED' in raw_status and 'UNUSED' not in raw_status:
                contact_status = Contact.UsageStatus.USED
            elif 'UNUSED' in raw_status:
                contact_status = Contact.UsageStatus.UNUSED
            elif raw_status in ['1', 'TRUE', 'YES', 'Y', 'COMPLETED', 'SUBMITTED', 'DONE']:
                contact_status = Contact.UsageStatus.USED
            elif data.get('used_at') or data.get('submission_uuid'):
                contact_status = Contact.UsageStatus.USED
            elif ent.status == 'USED':
                contact_status = Contact.UsageStatus.USED
            else:
                contact_status = Contact.UsageStatus.UNUSED

            # 8. ODK Submission metadata (UUID & timestamp)
            odk_sub_id = str(data.get('submission_uuid') or data.get('submissionId') or data.get('odk_submission_id') or '').strip()
            odk_sub_at_raw = data.get('used_at') or data.get('submitted_at') or None

            contact_defaults = {
                'name': name,
                'first_name': first_name,
                'last_name': last_name,
                'phone_number': phone,
                'job_id': job_id,
                'status': contact_status,
                'status_source': 'ODK_CENTRAL'
            }

            if odk_sub_id:
                contact_defaults['odk_submission_id'] = odk_sub_id
            if odk_sub_at_raw:
                try:
                    from django.utils.dateparse import parse_datetime
                    dt = parse_datetime(str(odk_sub_at_raw))
                    if dt:
                        contact_defaults['odk_submitted_at'] = dt
                except Exception:
                    pass

            contact, created = Contact.objects.update_or_create(
                email=email,
                defaults=contact_defaults
            )

            if password:
                contact.login_password = password
                contact.save(update_fields=['login_password_encrypted'])

            if group:
                group.contacts.add(contact)
                # Save values for all custom fields mapped or present on entity
                if custom_fields_by_key:
                    for attr_name, gcf in custom_fields_by_key.items():
                        val = data.get(attr_name)
                        if val is None and hasattr(ent, attr_name):
                            val = getattr(ent, attr_name)
                        if val is not None and str(val).strip() != '':
                            GroupContactValue.objects.update_or_create(
                                contact=contact,
                                field=gcf,
                                defaults={'value': str(val)}
                            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        if group:
            active_field_slugs = list(group.custom_fields.filter(is_active=True).values_list('slug', flat=True))
            current_selected = list(group.selected_fields or [])
            for s in active_field_slugs:
                if s not in current_selected:
                    current_selected.append(s)
            group.selected_fields = current_selected
            group.save(update_fields=['selected_fields'])

        return Response({
            'status': 'success',
            'created_count': created_count,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'total_imported': created_count + updated_count,
            'group_id': group.id if group else None,
            'group_name': group.name if group else None,
            'selected_fields': group.selected_fields if group else [],
            'message': f"Imported {created_count + updated_count} contacts ({created_count} created, {updated_count} updated, {skipped_count} skipped without valid email)."
        })



class ODKEntityViewSet(viewsets.ReadOnlyModelViewSet):
    """Viewset for individual ODK Entities."""
    queryset = ODKEntity.objects.all().order_by('-created_at')
    serializer_class = ODKEntitySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        dataset_id = self.request.query_params.get('dataset')
        if dataset_id:
            qs = qs.filter(dataset_id=dataset_id)
        return qs


class ODKSyncJobViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ODKSyncJob.objects.all().order_by('-started_at')
    serializer_class = ODKSyncJobSerializer
    permission_classes = [permissions.IsAuthenticated]


class ODKUnmatchedSubmissionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ODKUnmatchedSubmission.objects.all().order_by('-id')
    serializer_class = ODKUnmatchedSubmissionSerializer
    permission_classes = [permissions.IsAuthenticated]

