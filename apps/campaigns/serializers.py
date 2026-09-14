from rest_framework import serializers
from .models import Campaign, CampaignMessage
from apps.reminders.models import ReminderConfiguration


class ReminderConfigNestedSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReminderConfiguration
        fields = [
            'id', 'enabled', 'interval_value', 'interval_unit',
            'duration_value', 'duration_unit', 'max_reminders',
            'sync_odk_before_send', 'stop_when_used',
            'custom_subject', 'custom_html_content',
            'reminders_sent_count', 'last_run_at', 'next_run_at'
        ]


class CampaignSerializer(serializers.ModelSerializer):
    reminder_config = ReminderConfigNestedSerializer(required=False)
    sender_name = serializers.CharField(source='sender.name', read_only=True)
    sender_email = serializers.CharField(source='sender.email', read_only=True)
    odk_form_name = serializers.CharField(source='odk_form.name', read_only=True)
    odk_dataset_name = serializers.CharField(source='odk_dataset.name', read_only=True)
    total_messages_count = serializers.IntegerField(source='messages.count', read_only=True)

    class Meta:
        model = Campaign
        fields = [
            'id', 'name', 'campaign_type', 'description', 'status',
            'sender', 'sender_name', 'sender_email', 'odk_form', 'odk_form_name',
            'odk_dataset', 'odk_dataset_name',
            'completion_status_source', 'groups', 'initial_recipient_rule',
            'subject', 'preview_text', 'html_content', 'text_content',
            'track_opens', 'track_clicks', 'destination_url',
            'scheduled_at', 'started_at',
            'send_mode', 'batch_size', 'batch_interval_minutes',
            'completed_at', 'reminder_config', 'total_messages_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'status', 'started_at', 'completed_at', 'created_at', 'updated_at']

    def validate_destination_url(self, value):
        from apps.tracking.utils import validate_destination_url
        value = (value or '').strip()
        if value and not validate_destination_url(value):
            raise serializers.ValidationError(
                'Destination URL must be a valid absolute http:// or https:// URL.'
            )
        return value

    def create(self, validated_data):
        reminder_data = validated_data.pop('reminder_config', None)
        groups = validated_data.pop('groups', [])
        campaign = Campaign.objects.create(**validated_data)
        if groups:
            campaign.groups.set(groups)

        if reminder_data:
            ReminderConfiguration.objects.create(campaign=campaign, **reminder_data)
        else:
            ReminderConfiguration.objects.create(campaign=campaign)

        return campaign

    def update(self, instance, validated_data):
        reminder_data = validated_data.pop('reminder_config', None)
        groups = validated_data.pop('groups', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if groups is not None:
            instance.groups.set(groups)

        if reminder_data:
            rem_cfg, _ = ReminderConfiguration.objects.get_or_create(campaign=instance)
            for attr, val in reminder_data.items():
                setattr(rem_cfg, attr, val)
            rem_cfg.save()

        return instance


class CampaignMessageSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source='contact.name', read_only=True)
    contact_job_id = serializers.CharField(source='contact.job_id', read_only=True)

    class Meta:
        model = CampaignMessage
        fields = [
            'id', 'campaign', 'contact', 'contact_name', 'contact_job_id',
            'message_type', 'reminder_sequence', 'to_email', 'subject',
            'provider_message_id', 'status', 'skip_reason', 'error_message',
            'queued_at', 'sent_at', 'delivered_at', 'opened_at', 'clicked_at'
        ]
