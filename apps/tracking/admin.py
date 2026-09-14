from django.contrib import admin
from .models import CampaignTrackingLink, CampaignLinkClickEvent


@admin.register(CampaignTrackingLink)
class CampaignTrackingLinkAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'campaign', 'link_type', 'name', 'tracking_token', 'short_url',
        'is_active', 'click_count', 'human_click_count',
        'bot_click_count', 'last_clicked_at',
    )
    list_filter = ('link_type', 'is_active', 'campaign')
    search_fields = ('tracking_token', 'name', 'short_url', 'destination_url')
    readonly_fields = (
        'tracking_token', 'short_url', 'click_count', 'human_click_count',
        'bot_click_count', 'first_clicked_at', 'last_clicked_at',
        'created_at', 'updated_at',
    )


@admin.register(CampaignLinkClickEvent)
class CampaignLinkClickEventAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'link', 'campaign', 'clicked_at', 'click_type',
        'browser', 'operating_system', 'device_type', 'ip_address',
    )
    list_filter = ('click_type', 'campaign')
    search_fields = ('link__tracking_token', 'ip_address', 'user_agent')
    readonly_fields = (
        'link', 'campaign', 'clicked_at', 'ip_address', 'user_agent',
        'browser', 'operating_system', 'device_type', 'referrer',
        'click_type', 'metadata',
    )

    def has_add_permission(self, request):
        return False
