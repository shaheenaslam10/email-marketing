from rest_framework import serializers
from apps.tracking.models import CampaignTrackingLink
from apps.tracking.utils import validate_destination_url


class CampaignTrackingLinkSerializer(serializers.ModelSerializer):
    campaign_name = serializers.CharField(source='campaign.name', read_only=True)

    class Meta:
        model = CampaignTrackingLink
        fields = [
            'id', 'campaign', 'campaign_name', 'name',
            'tracking_token', 'short_url', 'destination_url',
            'is_active', 'click_count', 'human_click_count',
            'bot_click_count', 'first_clicked_at', 'last_clicked_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'campaign', 'tracking_token', 'short_url',
            'click_count', 'human_click_count', 'bot_click_count',
            'first_clicked_at', 'last_clicked_at',
            'created_at', 'updated_at',
        ]

    def validate_destination_url(self, value):
        value = (value or '').strip()
        if value and not validate_destination_url(value):
            raise serializers.ValidationError(
                'Destination URL must be a valid absolute http:// or https:// URL.'
            )
        return value
