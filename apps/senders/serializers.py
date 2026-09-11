from rest_framework import serializers
from .models import Sender


class SenderSerializer(serializers.ModelSerializer):
    password_or_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    provider_type_display = serializers.CharField(source='get_provider_type_display', read_only=True)

    class Meta:
        model = Sender
        fields = [
            'id', 'name', 'email', 'reply_to', 'provider_type', 'provider_type_display',
            'host', 'port', 'username', 'password_or_key', 'use_tls', 'use_ssl',
            'api_domain', 'api_region', 'is_active', 'daily_limit', 'hourly_limit',
            'per_minute_limit', 'today_sent', 'this_hour_sent', 'this_minute_sent',
            'last_sent_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'today_sent', 'this_hour_sent', 'this_minute_sent', 'last_sent_at', 'created_at', 'updated_at']

    def create(self, validated_data):
        raw_key = validated_data.pop('password_or_key', None)
        sender = super().create(validated_data)
        if raw_key:
            sender.password_or_key = raw_key
            sender.save(update_fields=['password_or_key_encrypted'])
        return sender

    def update(self, instance, validated_data):
        raw_key = validated_data.pop('password_or_key', None)
        sender = super().update(instance, validated_data)
        if raw_key is not None:
            sender.password_or_key = raw_key
            sender.save(update_fields=['password_or_key_encrypted'])
        return sender
