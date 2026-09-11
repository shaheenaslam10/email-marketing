from rest_framework import serializers
from .models import (
    ODKConnection, ODKProject, ODKForm, ODKFieldMapping,
    ODKSyncJob, ODKUnmatchedSubmission, ODKDataset, ODKEntity
)


class ODKConnectionSerializer(serializers.ModelSerializer):
    password_or_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = ODKConnection
        fields = [
            'id', 'name', 'base_url', 'username', 'password_or_token',
            'is_active', 'is_mock_sandbox', 'status', 'last_connection_test',
            'last_test_message', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'status', 'last_connection_test', 'last_test_message', 'created_at', 'updated_at']

    def create(self, validated_data):
        raw_token = validated_data.pop('password_or_token', None)
        conn = super().create(validated_data)
        if raw_token:
            conn.password_or_token = raw_token
            conn.save(update_fields=['password_or_token_encrypted'])
        return conn

    def update(self, instance, validated_data):
        raw_token = validated_data.pop('password_or_token', None)
        conn = super().update(instance, validated_data)
        if raw_token is not None:
            conn.password_or_token = raw_token
            conn.save(update_fields=['password_or_token_encrypted'])
        return conn


class ODKFieldMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ODKFieldMapping
        fields = ['id', 'form', 'job_id_field', 'email_field', 'phone_field', 'created_at', 'updated_at']


class ODKFormSerializer(serializers.ModelSerializer):
    field_mapping = ODKFieldMappingSerializer(read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = ODKForm
        fields = [
            'id', 'project', 'project_name', 'odk_xml_form_id', 'name', 'version',
            'submissions_count', 'last_sync_at', 'last_submission_timestamp',
            'field_mapping', 'created_at'
        ]


class ODKProjectSerializer(serializers.ModelSerializer):
    forms = ODKFormSerializer(many=True, read_only=True)

    class Meta:
        model = ODKProject
        fields = ['id', 'connection', 'odk_id', 'name', 'description', 'forms', 'created_at']


class ODKSyncJobSerializer(serializers.ModelSerializer):
    form_name = serializers.CharField(source='form.name', read_only=True)
    connection_name = serializers.CharField(source='connection.name', read_only=True)

    class Meta:
        model = ODKSyncJob
        fields = [
            'id', 'connection', 'connection_name', 'form', 'form_name',
            'status', 'trigger_source', 'submissions_checked', 'new_submissions',
            'contacts_marked_used', 'unmatched_job_ids', 'duration_seconds',
            'started_at', 'completed_at', 'error_message'
        ]


class ODKUnmatchedSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ODKUnmatchedSubmission
        fields = ['id', 'sync_job', 'submission_id', 'job_id_value', 'submitted_at', 'reason', 'raw_data']


class ODKEntitySerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source='dataset.name', read_only=True)

    class Meta:
        model = ODKEntity
        fields = [
            'id', 'dataset', 'dataset_name', 'uuid', 'label', 'status',
            'version', 'data', 'server_created_at', 'created_at', 'updated_at'
        ]


class ODKDatasetSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    connection_name = serializers.CharField(source='project.connection.name', read_only=True)
    connection_id = serializers.IntegerField(source='project.connection.id', read_only=True)

    class Meta:
        model = ODKDataset
        fields = [
            'id', 'project', 'project_name', 'connection_id', 'connection_name',
            'name', 'description', 'entities_count', 'unused_count', 'used_count',
            'last_entity_at', 'created_at', 'updated_at'
        ]

