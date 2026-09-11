from rest_framework import serializers
from .models import ReminderConfiguration, ReminderCycle


class ReminderCycleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReminderCycle
        fields = [
            'id', 'reminder_config', 'cycle_number', 'scheduled_for',
            'executed_at', 'status', 'eligible_count', 'sent_count',
            'delivered_count', 'opened_count', 'became_used_count',
            'still_unused_count', 'notes'
        ]


class ReminderConfigurationSerializer(serializers.ModelSerializer):
    cycles = ReminderCycleSerializer(many=True, read_only=True)
    campaign_name = serializers.CharField(source='campaign.name', read_only=True)

    class Meta:
        model = ReminderConfiguration
        fields = [
            'id', 'campaign', 'campaign_name', 'enabled', 'interval_value',
            'interval_unit', 'duration_value', 'duration_unit', 'max_reminders',
            'sync_odk_before_send', 'stop_when_used', 'reminder_template',
            'custom_subject', 'custom_html_content', 'reminders_sent_count',
            'last_run_at', 'next_run_at', 'cycles', 'created_at', 'updated_at'
        ]
