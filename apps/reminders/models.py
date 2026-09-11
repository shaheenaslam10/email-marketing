from django.db import models
from apps.campaigns.models import Campaign
from apps.email_templates.models import EmailTemplate


class ReminderConfiguration(models.Model):
    class Unit(models.TextChoices):
        DAYS = 'DAYS', 'Days'
        WEEKS = 'WEEKS', 'Weeks'

    campaign = models.OneToOneField(Campaign, on_delete=models.CASCADE, related_name='reminder_config')
    enabled = models.BooleanField(default=False)

    interval_value = models.PositiveIntegerField(default=2, help_text="e.g. 2")
    interval_unit = models.CharField(max_length=20, choices=Unit.choices, default=Unit.DAYS)

    duration_value = models.PositiveIntegerField(default=3, help_text="e.g. 3")
    duration_unit = models.CharField(max_length=20, choices=Unit.choices, default=Unit.WEEKS)

    max_reminders = models.PositiveIntegerField(default=10)
    sync_odk_before_send = models.BooleanField(default=True, help_text="Always sync ODK Central before running reminder")
    stop_when_used = models.BooleanField(default=True, help_text="Immediately stop sending reminders once contact status is USED")

    reminder_template = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    custom_subject = models.CharField(max_length=500, blank=True, help_text="e.g. Reminder: Complete your survey - {{job_id}}")
    custom_html_content = models.TextField(blank=True)

    reminders_sent_count = models.PositiveIntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Reminder for {self.campaign.name} (Every {self.interval_value} {self.interval_unit}, max {self.max_reminders})"


class ReminderCycle(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SYNCING_ODK = 'SYNCING_ODK', 'Syncing ODK'
        QUEUED = 'QUEUED', 'Queued'
        COMPLETED = 'COMPLETED', 'Completed'
        SKIPPED = 'SKIPPED', 'Skipped'
        FAILED = 'FAILED', 'Failed'

    reminder_config = models.ForeignKey(ReminderConfiguration, on_delete=models.CASCADE, related_name='cycles')
    cycle_number = models.PositiveIntegerField()  # 1, 2, 3...
    scheduled_for = models.DateTimeField()
    executed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Performance metrics for this cycle (Section 66)
    eligible_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    opened_count = models.PositiveIntegerField(default=0)
    became_used_count = models.PositiveIntegerField(default=0)
    still_unused_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['cycle_number']
        unique_together = ('reminder_config', 'cycle_number')

    def __str__(self):
        return f"{self.reminder_config.campaign.name} - Reminder Cycle #{self.cycle_number}"
