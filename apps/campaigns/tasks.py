from celery import shared_task
from apps.campaigns.models import Campaign
from apps.reminders.services import launch_initial_campaign, process_single_campaign_message


@shared_task
def task_launch_campaign(campaign_id: int):
    try:
        campaign = Campaign.objects.get(id=campaign_id)
        return launch_initial_campaign(campaign)
    except Campaign.DoesNotExist:
        return 0


@shared_task
def task_send_message(message_id: int):
    return process_single_campaign_message(message_id)
