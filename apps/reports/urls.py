from django.urls import path
from .views import (
    CampaignReportView, CampaignReportMessagesView,
    CampaignReportLinkRecipientsView, CampaignReportLinkClicksView,
    CampaignReportShareableLinksView,
    CampaignExportView, DashboardStatsView, ReportXlsxExportView
)

urlpatterns = [
    path('campaigns/<int:pk>/report/', CampaignReportView.as_view(), name='campaign_report'),
    path('campaigns/<int:pk>/report/messages/', CampaignReportMessagesView.as_view(), name='campaign_report_messages'),
    path('campaigns/<int:pk>/report/link-recipients/', CampaignReportLinkRecipientsView.as_view(), name='campaign_report_link_recipients'),
    path('campaigns/<int:pk>/report/link-clicks/', CampaignReportLinkClicksView.as_view(), name='campaign_report_link_clicks'),
    path('campaigns/<int:pk>/report/shareable-links/', CampaignReportShareableLinksView.as_view(), name='campaign_report_shareable_links'),
    path('campaigns/<int:pk>/export/<str:fmt>/', CampaignExportView.as_view(), name='campaign_export'),
    path('campaigns/report/xlsx/', ReportXlsxExportView.as_view(), name='campaign_report_xlsx'),
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard_stats'),
]
