from rest_framework import serializers
from .models import ContactGroup, GroupCustomField, GroupContactValue, TestEmailGroup


class TestEmailGroupSerializer(serializers.ModelSerializer):
    email_list = serializers.ListField(read_only=True)
    recipient_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TestEmailGroup
        fields = [
            'id', 'name', 'description', 'emails',
            'email_list', 'recipient_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'email_list', 'recipient_count']


class GroupCustomFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupCustomField
        fields = ['id', 'name', 'slug', 'field_type', 'description', 'options', 'is_required', 'is_active', 'order', 'created_at']
        read_only_fields = ['id', 'slug', 'created_at']


class GroupContactValueSerializer(serializers.ModelSerializer):
    field_id = serializers.IntegerField(source='field.id', read_only=True)
    field_slug = serializers.ReadOnlyField(source='field.slug')
    field_name = serializers.ReadOnlyField(source='field.name')

    class Meta:
        model = GroupContactValue
        fields = ['id', 'field_id', 'field_slug', 'field_name', 'value']


class ContactGroupSerializer(serializers.ModelSerializer):
    contact_count = serializers.IntegerField(read_only=True)
    unused_count = serializers.IntegerField(read_only=True)
    used_count = serializers.IntegerField(read_only=True)
    custom_fields = GroupCustomFieldSerializer(many=True, read_only=True)

    class Meta:
        model = ContactGroup
        fields = [
            'id', 'name', 'description', 'selected_fields', 'field_mappings',
            'contact_count', 'unused_count', 'used_count', 'custom_fields',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

