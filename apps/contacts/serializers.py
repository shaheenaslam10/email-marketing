from rest_framework import serializers
from .models import Contact, ContactStatusHistory, ContactCustomField, ContactCustomValue


class ContactCustomValueSerializer(serializers.ModelSerializer):
    field_slug = serializers.ReadOnlyField(source='field.slug')
    field_name = serializers.ReadOnlyField(source='field.name')

    class Meta:
        model = ContactCustomValue
        fields = ['id', 'field', 'field_slug', 'field_name', 'value']


class ContactStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_username = serializers.ReadOnlyField(source='changed_by.username')

    class Meta:
        model = ContactStatusHistory
        fields = ['id', 'old_status', 'new_status', 'changed_at', 'source', 'odk_submission_id', 'changed_by_username', 'notes']


class ContactSerializer(serializers.ModelSerializer):
    # Sensitive field: write_only by default so it's not exposed in general listing APIs
    login_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    custom_values = ContactCustomValueSerializer(many=True, read_only=True)
    group_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        read_only=True,
        source='groups'
    )
    url = serializers.SerializerMethodField()

    class Meta:
        model = Contact
        fields = [
            'id', 'name', 'first_name', 'last_name', 'email', 'phone_number',
            'login_password', 'job_id', 'status', 'email_status',
            'odk_submission_id', 'odk_submitted_at', 'odk_last_checked_at',
            'status_source', 'last_email_sent_at', 'last_reminder_sent_at',
            'reminder_count', 'unsubscribed', 'bounce_status', 'created_at',
            'updated_at', 'custom_values', 'group_ids', 'url'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'odk_submission_id', 'odk_submitted_at', 'url']

    def get_url(self, obj) -> str:
        # Check group custom values
        if hasattr(obj, 'group_custom_values'):
            for gv in obj.group_custom_values.select_related('field').all():
                if gv.field.slug == 'url' or gv.field.field_type == 'URL':
                    val = (gv.value or '').strip()
                    if not val and gv.field.description and gv.field.description.strip().startswith(('http://', 'https://')):
                        val = gv.field.description.strip()
                    if val:
                        return val
        # Check group custom field definitions fallback
        for grp in obj.groups.prefetch_related('custom_fields').all():
            for f in grp.custom_fields.all():
                if f.slug == 'url' or f.field_type == 'URL':
                    desc = (f.description or '').strip()
                    if desc.startswith(('http://', 'https://')):
                        return desc
        return ''

    def create(self, validated_data):
        password = validated_data.pop('login_password', None)
        group_id = self.initial_data.get('group_id')
        group_ids = list(self.initial_data.get('group_ids', []))
        if group_id:
            group_ids.append(group_id)

        contact = super().create(validated_data)
        if password:
            contact.login_password = password
            contact.save(update_fields=['login_password_encrypted'])

        if group_ids:
            from apps.groups.models import ContactGroup
            groups = ContactGroup.objects.filter(id__in=group_ids)
            for g in groups:
                g.contacts.add(contact)

        return contact

    def update(self, instance, validated_data):
        password = validated_data.pop('login_password', None)
        old_status = instance.status
        contact = super().update(instance, validated_data)
        if password is not None and password != '':
            contact.login_password = password
            contact.save(update_fields=['login_password_encrypted'])

        # Keep display name in sync with first/last name
        if 'first_name' in validated_data or 'last_name' in validated_data:
            full_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
            if full_name:
                contact.name = full_name
                contact.save(update_fields=['name'])

        # Handle group assignment in update
        if 'group_id' in self.initial_data or 'group_ids' in self.initial_data:
            from apps.groups.models import ContactGroup
            target_ids = set()
            gid = self.initial_data.get('group_id')
            if gid:
                target_ids.add(int(gid))
            gids = self.initial_data.get('group_ids')
            if gids and isinstance(gids, list):
                for g in gids:
                    if g:
                        target_ids.add(int(g))
            contact.groups.set(ContactGroup.objects.filter(id__in=target_ids))

        # Record manual status change if updated
        if 'status' in validated_data and validated_data['status'] != old_status:
            user = self.context.get('request').user if self.context.get('request') else None
            ContactStatusHistory.objects.create(
                contact=contact,
                old_status=old_status,
                new_status=contact.status,
                source='MANUAL',
                changed_by=user if user and user.is_authenticated else None,
                notes="Status changed via API"
            )
        return contact


class ContactCustomFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactCustomField
        fields = ['id', 'name', 'slug', 'field_type', 'description', 'created_at']
