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

    def validate(self, attrs):
        # A link with neither its own destination nor a campaign default
        # can never redirect (/c/ returns 400). Refuse to create or update
        # into that dead state. Campaign comes from the view context on
        # create, or from the instance on update.
        campaign = None
        if self.instance is not None:
            campaign = self.instance.campaign
        elif self.context.get('campaign') is not None:
            campaign = self.context['campaign']
        if 'destination_url' in attrs:
            # Present even when explicitly blanked: use it as given.
            dest = (attrs.get('destination_url') or '').strip()
        elif self.instance is not None:
            # Field untouched on update (PATCH without it): keep stored value.
            dest = (self.instance.destination_url or '').strip()
        else:
            dest = ''
        campaign_default = ''
        if campaign is not None:
            campaign_default = (campaign.destination_url or '').strip()
        if not dest and not campaign_default:
            raise serializers.ValidationError({
                'destination_url': (
                    'Set a destination URL for this link or a default '
                    'destination URL on the campaign; otherwise the link '
                    'cannot redirect.'
                ),
            })
        return attrs
