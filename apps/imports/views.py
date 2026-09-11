import os
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from .models import ImportJob
from .services import read_file_rows, analyze_import, execute_import
from apps.groups.models import ContactGroup


class ImportUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file uploaded'}, status=status.HTTP_400_BAD_REQUEST)

        filename = uploaded_file.name
        file_ext = os.path.splitext(filename)[1].lower()
        if file_ext not in ['.csv', '.xlsx']:
            return Response({'error': 'Only CSV and XLSX files are supported'}, status=status.HTTP_400_BAD_REQUEST)

        file_type = 'XLSX' if file_ext == '.xlsx' else 'CSV'
        import_job = ImportJob.objects.create(
            file=uploaded_file,
            file_type=file_type,
            created_by=request.user
        )

        try:
            headers, rows = read_file_rows(import_job.file.path, file_type)
            import_job.total_rows = len(rows)
            import_job.save(update_fields=['total_rows'])

            # Auto-suggest field mappings based on standard header names
            suggested_mapping = {}
            for h in headers:
                h_lower = h.lower()
                if any(x in h_lower for x in ['email', 'e-mail', 'mail']):
                    suggested_mapping['email'] = h
                elif any(x in h_lower for x in ['job', 'respondent_id', 'jobid', 'job_id', 'job number']):
                    suggested_mapping['job_id'] = h
                elif any(x in h_lower for x in ['first_name', 'firstname', 'first name']):
                    suggested_mapping['first_name'] = h
                elif any(x in h_lower for x in ['last_name', 'lastname', 'last name', 'surname']):
                    suggested_mapping['last_name'] = h
                elif any(x in h_lower for x in ['name', 'full_name', 'respondent name', 'fullname']):
                    suggested_mapping['name'] = h
                elif any(x in h_lower for x in ['phone', 'mobile', 'cell', 'tel']):
                    suggested_mapping['phone_number'] = h
                elif any(x in h_lower for x in ['password', 'login_password', 'pass', 'pwd']):
                    suggested_mapping['login_password'] = h
                elif 'status' in h_lower:
                    suggested_mapping['status'] = h

            return Response({
                'import_job_id': import_job.id,
                'file_type': file_type,
                'headers': headers,
                'suggested_mapping': suggested_mapping,
                'sample_rows': rows[:5],
                'total_rows': len(rows),
            })
        except Exception as e:
            import_job.status = ImportJob.Status.FAILED
            import_job.error_message = str(e)
            import_job.save()
            return Response({'error': f'Failed to parse file: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)


class ImportAnalyzeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            import_job = ImportJob.objects.get(pk=pk)
        except ImportJob.DoesNotExist:
            return Response({'error': 'Import job not found'}, status=status.HTTP_404_NOT_FOUND)

        field_mapping = request.data.get('field_mapping', {})
        if not field_mapping.get('email'):
            return Response({'error': 'Email field mapping is required'}, status=status.HTTP_400_BAD_REQUEST)

        headers, rows = read_file_rows(import_job.file.path, import_job.file_type)
        stats = analyze_import(rows, field_mapping)

        import_job.field_mapping = field_mapping
        import_job.valid_contacts = stats['valid_contacts']
        import_job.new_contacts = stats['new_contacts']
        import_job.existing_contacts = stats['existing_contacts']
        import_job.duplicate_rows = stats['duplicate_rows']
        import_job.invalid_emails = stats['invalid_emails']
        import_job.missing_emails = stats['missing_emails']
        import_job.missing_job_ids = stats['missing_job_ids']
        import_job.status = ImportJob.Status.ANALYZED
        import_job.save()

        return Response(stats)


class ImportExecuteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            import_job = ImportJob.objects.get(pk=pk)
        except ImportJob.DoesNotExist:
            return Response({'error': 'Import job not found'}, status=status.HTTP_404_NOT_FOUND)

        field_mapping = request.data.get('field_mapping', import_job.field_mapping)
        duplicate_strategy = request.data.get('duplicate_strategy', import_job.duplicate_strategy)
        target_group_id = request.data.get('target_group_id')
        new_group_name = request.data.get('new_group_name')

        target_group = None
        if target_group_id:
            target_group = ContactGroup.objects.filter(id=target_group_id).first()
        elif new_group_name:
            target_group, _ = ContactGroup.objects.get_or_create(name=new_group_name.strip())

        headers, rows = read_file_rows(import_job.file.path, import_job.file_type)
        
        import_job.status = ImportJob.Status.PROCESSING
        import_job.save()

        try:
            result = execute_import(
                rows=rows,
                field_mapping=field_mapping,
                duplicate_strategy=duplicate_strategy,
                target_group=target_group,
                user=request.user
            )
            import_job.status = ImportJob.Status.COMPLETED
            import_job.target_group = target_group
            import_job.save()

            if target_group and field_mapping:
                mapped_keys = [k for k, v in field_mapping.items() if v]
                if 'email' not in mapped_keys:
                    mapped_keys.insert(0, 'email')
                target_group.selected_fields = mapped_keys
                target_group.field_mappings = field_mapping
                target_group.save(update_fields=['selected_fields', 'field_mappings'])

            return Response({
                'message': 'Import completed successfully',
                'result': result,
                'target_group': target_group.name if target_group else None,
                'selected_fields': target_group.selected_fields if target_group else []
            })
        except Exception as e:
            import_job.status = ImportJob.Status.FAILED
            import_job.error_message = str(e)
            import_job.save()
            return Response({'error': f'Import execution failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
