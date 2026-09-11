from django.urls import path
from .views import CampaignReminderView, ReminderPreviewEligibilityView, ReminderSendNowView

urlpatterns = [
    path('campaigns/<int:campaign_id>/reminders/', CampaignReminderView.as_view(), name='campaign_reminders'),
    path('campaigns/<int:campaign_id>/reminders/eligibility/', ReminderPreviewEligibilityView.as_view(), name='reminder_eligibility'),
    path('campaigns/<int:campaign_id>/reminders/send-now/', ReminderSendNowView.as_view(), name='reminder_send_now'),
]
