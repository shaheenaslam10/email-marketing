from celery import shared_task
from django.utils import timezone
from apps.campaigns.models import Campaign
from apps.reminders.models import ReminderConfiguration
from .services import execute_reminder_cycle


@shared_task
def task_run_campaign_reminder(campaign_id: int):
    try:
        campaign = Campaign.objects.get(id=campaign_id)
        cycle = execute_reminder_cycle(campaign)
        return f"Completed reminder cycle #{cycle.cycle_number} for campaign {campaign.id}"
    except Exception as e:
        return f"Reminder failed: {str(e)}"


@shared_task
def task_check_scheduled_reminders():
    """Periodic scheduler task checking for due automated reminders."""
    now = timezone.now()
    due_configs = ReminderConfiguration.objects.filter(
        enabled=True,
        campaign__status=Campaign.Status.ACTIVE,
        next_run_at__lte=now
    )
    results = []
    for cfg in due_configs:
        try:
            cycle = execute_reminder_cycle(cfg.campaign)
            results.append(f"Campaign {cfg.campaign.id}: cycle #{cycle.cycle_number} run")
        except Exception as e:
            results.append(f"Campaign {cfg.campaign.id} error: {str(e)}")
    return results


@shared_task
def task_check_scheduled_campaigns():
    """Periodic scheduler task checking for due scheduled campaigns."""
    from .services import launch_initial_campaign
    now = timezone.now()
    due_campaigns = Campaign.objects.filter(
        status=Campaign.Status.SCHEDULED,
        scheduled_at__lte=now
    )
    results = []
    for c in due_campaigns:
        try:
            b_size = c.batch_size if c.send_mode == Campaign.SendMode.BATCHES else None
            dispatched = launch_initial_campaign(c, batch_size=b_size)
            results.append(f"Campaign {c.id} launched ({dispatched} dispatched)")
        except Exception as e:
            results.append(f"Campaign {c.id} launch error: {str(e)}")
    return results
