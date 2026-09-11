from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .models import ContactGroup, GroupCustomField, GroupContactValue, TestEmailGroup
from .serializers import (
    ContactGroupSerializer, GroupCustomFieldSerializer,
    GroupContactValueSerializer, TestEmailGroupSerializer
)
from apps.contacts.models import Contact
from apps.contacts.serializers import ContactSerializer
from apps.audit.models import AuditLog


class ContactGroupViewSet(viewsets.ModelViewSet):
    queryset = ContactGroup.objects.all()
    serializer_class = ContactGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def destroy(self, request, *args, **kwargs):
        group = self.get_object()
        group_id = group.id
        group_name = group.name

        delete_contacts = (
            request.query_params.get('delete_contacts', '').lower() in ['true', '1', 'yes'] or
            (isinstance(request.data, dict) and request.data.get('delete_contacts') in [True, 'true', '1'])
        )

        contacts_count = group.contacts.count()
        if delete_contacts:
            contacts = list(group.contacts.all())
            for c in contacts:
                c.delete()

        group.delete()

        try:
            AuditLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                action="GROUP_DELETED",
                entity_type="ContactGroup",
                entity_id=str(group_id),
                details={
                    "group_name": group_name,
                    "contacts_deleted": contacts_count if delete_contacts else 0,
                    "kept_contacts": not delete_contacts
                }
            )
        except Exception:
            pass

        return Response({
            'message': f"Group '{group_name}' was successfully deleted.",
            'deleted_group_id': group_id,
            'contacts_deleted': contacts_count if delete_contacts else 0
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def contacts(self, request, pk=None):
        group = self.get_object()
        contacts = group.contacts.all().order_by('-created_at')

        status_param = request.query_params.get('status')
        if status_param:
            contacts = contacts.filter(status=status_param.upper())

        page = self.paginate_queryset(contacts)
        if page is not None:
            serializer = ContactSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = ContactSerializer(contacts, many=True)
        return Response(serializer.data)

    # ─── Group Custom Fields ──────────────────────────────────────────────────

    @action(detail=True, methods=['get', 'post'], url_path='fields')
    def fields(self, request, pk=None):
        """List or create custom fields for this group."""
        group = self.get_object()
        if request.method == 'POST':
            serializer = GroupCustomFieldSerializer(data=request.data)
            if serializer.is_valid():
                field = serializer.save(group=group)
                # If description is a URL or default value, populate for contacts in this group
                desc = (field.description or '').strip()
                if desc and (desc.startswith(('http://', 'https://')) or field.field_type == 'URL'):
                    for contact in group.contacts.all():
                        GroupContactValue.objects.get_or_create(
                            contact=contact,
                            field=field,
                            defaults={'value': desc}
                        )
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        fields = group.custom_fields.all()
        if request.query_params.get('active', '').lower() in ('true', '1', 'yes'):
            fields = fields.filter(is_active=True)
        return Response(GroupCustomFieldSerializer(fields, many=True).data)

    @action(detail=True, methods=['patch', 'put', 'delete'], url_path=r'fields/(?P<field_id>\d+)')
    def manage_field(self, request, pk=None, field_id=None):
        """Update or delete a specific custom field in this group."""
        group = self.get_object()
        field = get_object_or_404(GroupCustomField, id=field_id, group=group)

        if request.method == 'DELETE':
            field.delete()
            return Response({'message': 'Field deleted'}, status=status.HTTP_200_OK)

        serializer = GroupCustomFieldSerializer(field, data=request.data, partial=True)
        if serializer.is_valid():
            field = serializer.save()
            # If description is a URL or default value, update contacts with empty values in this group
            desc = (field.description or '').strip()
            if desc and (desc.startswith(('http://', 'https://')) or field.field_type == 'URL'):
                for contact in group.contacts.all():
                    gcv, created = GroupContactValue.objects.get_or_create(
                        contact=contact,
                        field=field,
                        defaults={'value': desc}
                    )
                    if not created and not (gcv.value or '').strip():
                        gcv.value = desc
                        gcv.save(update_fields=['value'])
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path=r'fields/(?P<field_id>\d+)/toggle')
    def toggle_field_active(self, request, pk=None, field_id=None):
        """Toggle the is_active status of a custom field."""
        group = self.get_object()
        field = get_object_or_404(GroupCustomField, id=field_id, group=group)
        field.is_active = not field.is_active
        field.save(update_fields=['is_active'])
        return Response({
            'id': field.id,
            'is_active': field.is_active,
            'message': f'Field "{field.name}" is now {"active" if field.is_active else "inactive"}'
        })

    @action(detail=True, methods=['post'], url_path=r'fields/reorder')
    def reorder_fields(self, request, pk=None):
        """Reorder custom fields by passing [{id: X, order: Y}, ...]"""
        group = self.get_object()
        items = request.data.get('fields', [])
        for item in items:
            GroupCustomField.objects.filter(id=item['id'], group=group).update(order=item['order'])
        fields = group.custom_fields.all()
        return Response(GroupCustomFieldSerializer(fields, many=True).data)

    # ─── Group Contact Field Values ───────────────────────────────────────────

    @action(detail=True, methods=['get'], url_path=r'contact-values/(?P<contact_id>\d+)')
    def get_contact_values(self, request, pk=None, contact_id=None):
        """Get all group-field values for a specific contact in this group."""
        group = self.get_object()
        contact = get_object_or_404(Contact, id=contact_id)
        values = GroupContactValue.objects.filter(field__group=group, contact=contact)
        return Response(GroupContactValueSerializer(values, many=True).data)

    @action(detail=True, methods=['post'], url_path=r'contact-values/(?P<contact_id>\d+)/save')
    def save_contact_values(self, request, pk=None, contact_id=None):
        """
        Save group-specific field values for a contact.
        Payload: { "values": { "field_slug": "value", ... } }
        """
        group = self.get_object()
        contact = get_object_or_404(Contact, id=contact_id)
        values_data = request.data.get('values', {})

        saved = []
        for slug, value in values_data.items():
            try:
                field = GroupCustomField.objects.get(group=group, slug=slug)
                obj, created = GroupContactValue.objects.update_or_create(
                    contact=contact,
                    field=field,
                    defaults={'value': str(value) if value is not None else ''}
                )
                saved.append({'slug': slug, 'value': obj.value, 'created': created})
            except GroupCustomField.DoesNotExist:
                pass

        return Response({'saved': saved, 'count': len(saved)})


class TestEmailGroupViewSet(viewsets.ModelViewSet):
    """API for managing multi-email testing groups."""
    queryset = TestEmailGroup.objects.all()
    serializer_class = TestEmailGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

