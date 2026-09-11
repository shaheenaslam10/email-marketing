import uuid
from django.db import models
from apps.campaigns.models import CampaignMessage, Campaign
from apps.contacts.models import Contact


class TrackingToken(models.Model):
    token = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.OneToOneField(CampaignMessage, on_delete=models.CASCADE, related_name='tracking_token')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Token for msg {self.message.id} ({self.token})"


class EmailEvent(models.Model):
    class EventType(models.TextChoices):
        QUEUED = 'QUEUED', 'Queued'
        SENT = 'SENT', 'Sent'
        DELIVERED = 'DELIVERED', 'Delivered'
        OPEN = 'OPEN', 'Open'
        CLICK = 'CLICK', 'Click'
        UNSUBSCRIBE = 'UNSUBSCRIBE', 'Unsubscribe'
        SOFT_BOUNCE = 'SOFT_BOUNCE', 'Soft Bounce'
        HARD_BOUNCE = 'HARD_BOUNCE', 'Hard Bounce'
        COMPLAINT = 'COMPLAINT', 'Spam Complaint'
        SKIPPED = 'SKIPPED', 'Skipped'

    message = models.ForeignKey(CampaignMessage, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=30, choices=EventType.choices, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    url_clicked = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['event_type', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.event_type} on msg #{self.message.id} at {self.timestamp}"


class UnsubscribeRecord(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='unsubscribes')
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True)
    reason = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.contact.email} unsubscribed at {self.timestamp}"


class ShortenedLink(models.Model):
    """
    Represents an original campaign destination URL converted into a branded short URL.
    (Requirement 1 & 12)
    """
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name='shortened_links')
    original_url = models.TextField(help_text="Target destination URL (http:// or https://)")
    link_name = models.CharField(max_length=255, blank=True, help_text="Human readable link label (e.g. ODK Survey Link)")
    tracking_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'shortened_links'
        ordering = ['-created_at']

    def __str__(self):
        label = self.link_name or self.original_url[:40]
        return f"ShortenedLink #{self.id}: {label}"


class RecipientLink(models.Model):
    """
    Unique recipient-specific tracking link for every: Campaign + Contact + ShortenedLink.
    (Requirement 2 & 12)
    """
    shortened_link = models.ForeignKey(ShortenedLink, on_delete=models.CASCADE, related_name='recipient_links')
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, null=True, blank=True, related_name='recipient_links')
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='recipient_links')
    tracking_token = models.CharField(max_length=16, unique=True, db_index=True)
    short_url = models.CharField(max_length=500)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    last_clicked_at = models.DateTimeField(null=True, blank=True)
    click_count = models.PositiveIntegerField(default=0)
    human_click_count = models.PositiveIntegerField(default=0)
    bot_click_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'recipient_links'
        ordering = ['-created_at']
        unique_together = [('shortened_link', 'contact', 'campaign')]
        indexes = [
            models.Index(fields=['tracking_token']),
            models.Index(fields=['campaign', 'contact']),
            models.Index(fields=['campaign', 'shortened_link']),
        ]

    def __str__(self):
        return f"RecipientLink {self.tracking_token} for {self.contact.email} ({self.shortened_link.link_name or self.shortened_link.id})"

    @property
    def has_clicked(self):
        return self.click_count > 0

    @property
    def has_human_clicked(self):
        return self.human_click_count > 0


class LinkClickEvent(models.Model):
    """
    Granular click-level audit record for every link interaction.
    (Requirement 4, 12, 14)
    """
    class ClickType(models.TextChoices):
        HUMAN = 'HUMAN', 'Human'
        SUSPECTED_BOT = 'SUSPECTED_BOT', 'Suspected Bot'
        UNKNOWN = 'UNKNOWN', 'Unknown'

    recipient_link = models.ForeignKey(RecipientLink, on_delete=models.CASCADE, related_name='click_events')
    campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name='link_click_events')
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='link_click_events')
    clicked_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    browser = models.CharField(max_length=100, blank=True)
    operating_system = models.CharField(max_length=100, blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    referrer = models.TextField(null=True, blank=True)
    click_type = models.CharField(max_length=30, choices=ClickType.choices, default=ClickType.HUMAN, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'link_click_events'
        ordering = ['-clicked_at']
        indexes = [
            models.Index(fields=['campaign', 'clicked_at']),
            models.Index(fields=['contact', 'clicked_at']),
            models.Index(fields=['click_type', 'clicked_at']),
        ]

    def __str__(self):
        return f"Click [{self.click_type}] on {self.recipient_link.tracking_token} at {self.clicked_at}"
