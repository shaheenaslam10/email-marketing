from django.urls import path
from .views import (
    CampaignReportView, CampaignReportMessagesView,
    CampaignReportLinkRecipientsView, CampaignExportView, DashboardStatsView
)

urlpatterns = [
    path('campaigns/<int:pk>/report/', CampaignReportView.as_view(), name='campaign_report'),
    path('campaigns/<int:pk>/report/messages/', CampaignReportMessagesView.as_view(), name='campaign_report_messages'),
    path('campaigns/<int:pk>/report/link-recipients/', CampaignReportLinkRecipientsView.as_view(), name='campaign_report_link_recipients'),
    path('campaigns/<int:pk>/export/<str:fmt>/', CampaignExportView.as_view(), name='campaign_export'),
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard_stats'),
]
