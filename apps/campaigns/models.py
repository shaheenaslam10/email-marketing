from django.db import models
from django.conf import settings
from apps.senders.models import Sender
from apps.groups.models import ContactGroup
from apps.contacts.models import Contact
from apps.odk.models import ODKForm, ODKDataset


class Campaign(models.Model):
    class Type(models.TextChoices):
        STANDARD = 'STANDARD', 'Standard Email Campaign'
        SURVEY_REMINDER = 'SURVEY_REMINDER', 'Survey Campaign with Automated Reminders'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SCHEDULED = 'SCHEDULED', 'Scheduled'
        ACTIVE = 'ACTIVE', 'Active'
        PAUSED = 'PAUSED', 'Paused'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class InitialRecipientRule(models.TextChoices):
        UNUSED_ONLY = 'UNUSED_ONLY', 'Unused contacts only'
        ALL_VALID = 'ALL_VALID', 'All selected valid contacts'

    name = models.CharField(max_length=255)
    campaign_type = models.CharField(max_length=30, choices=Type.choices, default=Type.SURVEY_REMINDER)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    sender = models.ForeignKey(Sender, on_delete=models.PROTECT, related_name='campaigns')
    odk_form = models.ForeignKey(ODKForm, on_delete=models.SET_NULL, null=True, blank=True, related_name='campaigns')
    odk_dataset = models.ForeignKey(ODKDataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='campaigns')
    completion_status_source = models.CharField(max_length=50, default='ODK_CENTRAL')

    groups = models.ManyToManyField(ContactGroup, related_name='campaigns', blank=True)
    initial_recipient_rule = models.CharField(
        max_length=30,
        choices=InitialRecipientRule.choices,
        default=InitialRecipientRule.UNUSED_ONLY
    )

    subject = models.CharField(max_length=500)
    preview_text = models.CharField(max_length=255, blank=True)
    html_content = models.TextField()
    text_content = models.TextField(blank=True)

    track_opens = models.BooleanField(default=True)
    track_clicks = models.BooleanField(default=True)

    class SendMode(models.TextChoices):
        IMMEDIATE = 'IMMEDIATE', 'Send Now'
        SCHEDULED = 'SCHEDULED', 'Schedule for later'
        BATCHES = 'BATCHES', 'Send in Batches'
        DRAFT = 'DRAFT', 'Draft'

    send_mode = models.CharField(max_length=20, choices=SendMode.choices, default=SendMode.IMMEDIATE)
    batch_size = models.PositiveIntegerField(default=50, null=True, blank=True)
    batch_interval_minutes = models.PositiveIntegerField(default=30, null=True, blank=True)

    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class CampaignMessage(models.Model):
    class MessageType(models.TextChoices):
        INITIAL = 'INITIAL', 'Initial Email'
        REMINDER = 'REMINDER', 'Reminder Email'
        TEST = 'TEST', 'Test Email'

    class Status(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        SENT = 'SENT', 'Sent'
        DELIVERED = 'DELIVERED', 'Delivered'
        OPENED = 'OPENED', 'Opened'
        CLICKED = 'CLICKED', 'Clicked'
        SOFT_BOUNCE = 'SOFT_BOUNCE', 'Soft Bounce'
        HARD_BOUNCE = 'HARD_BOUNCE', 'Hard Bounce'
        FAILED = 'FAILED', 'Failed'
        SKIPPED = 'SKIPPED', 'Skipped'

    class SkipReason(models.TextChoices):
        CONTACT_USED = 'CONTACT_USED', 'Contact became Used before reminder send'
        UNSUBSCRIBED = 'UNSUBSCRIBED', 'Contact is unsubscribed'
        HARD_BOUNCE = 'HARD_BOUNCE', 'Contact hard bounced'
        BLOCKED = 'BLOCKED', 'Contact is blocked'
        INVALID_EMAIL = 'INVALID_EMAIL', 'Invalid email address'
        CAMPAIGN_PAUSED = 'CAMPAIGN_PAUSED', 'Campaign is paused'
        CAMPAIGN_ENDED = 'CAMPAIGN_ENDED', 'Campaign has ended'
        MAX_REMINDERS_REACHED = 'MAX_REMINDERS_REACHED', 'Maximum reminders reached'

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='messages')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='campaign_messages')
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.INITIAL)
    reminder_sequence = models.PositiveIntegerField(default=0, help_text="0 for initial, 1 for reminder 1, 2 for reminder 2...")

    to_email = models.EmailField()
    subject = models.CharField(max_length=500)
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    retry_count = models.PositiveIntegerField(default=0)
    skip_reason = models.CharField(max_length=50, blank=True)
    error_message = models.TextField(blank=True)

    queued_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-queued_at']
        # Section 54: Duplicate-send protection unique constraint
        unique_together = ('campaign', 'contact', 'message_type', 'reminder_sequence')
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['contact', 'status']),
            models.Index(fields=['provider_message_id']),
        ]

    def __str__(self):
        return f"{self.campaign.name} -> {self.to_email} ({self.message_type} #{self.reminder_sequence}) [{self.status}]"
