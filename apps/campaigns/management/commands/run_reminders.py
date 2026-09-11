from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.campaigns.models import Campaign
from apps.reminders.models import ReminderConfiguration
from apps.reminders.services import execute_reminder_cycle


class Command(BaseCommand):
    help = "Checks all active campaigns with enabled automated reminders and executes due reminder cycles."

    def add_arguments(self, parser):
        parser.add_argument('--campaign-id', type=int, help='Run reminders specifically for a single campaign ID')
        parser.add_argument('--force', action='store_true', help='Force execute cycle even if next_run_at is not reached')

    def handle(self, *args, **options):
        campaign_id = options.get('campaign_id')
        force = options.get('force', False)
        now = timezone.now()

        if campaign_id:
            configs = ReminderConfiguration.objects.filter(campaign_id=campaign_id)
        else:
            if force:
                configs = ReminderConfiguration.objects.filter(enabled=True, campaign__status=Campaign.Status.ACTIVE)
            else:
                configs = ReminderConfiguration.objects.filter(
                    enabled=True,
                    campaign__status=Campaign.Status.ACTIVE,
                    next_run_at__lte=now
                )

        count = configs.count()
        self.stdout.write(f"Found {count} campaign(s) eligible for automated reminders.")

        for cfg in configs:
            self.stdout.write(f"Processing reminders for Campaign #{cfg.campaign.id}: '{cfg.campaign.name}'...")
            try:
                cycle = execute_reminder_cycle(cfg.campaign, manual_trigger=force)
                self.stdout.write(self.style.SUCCESS(
                    f"-> Completed Cycle #{cycle.cycle_number}: {cycle.sent_count} sent, "
                    f"{cycle.eligible_count} eligible, {cycle.became_used_count} converted to USED."
                ))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"-> Failed: {str(e)}"))
