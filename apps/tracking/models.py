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


class CampaignTrackingLink(models.Model):
    """
    Unified branded short link served at /c/<token>/.

    Two types share one table, one endpoint and one analytics pipeline:

    * SHAREABLE - a single stable URL per link, generated by the campaign
      owner in the editor and distributed externally (social media, ads,
      partner sites, QR codes, copy/paste). Visits are anonymous: no
      contact association, aggregate counts only.
    * RECIPIENT - one unique URL per (campaign, contact, destination),
      generated automatically at email dispatch by wrap_tracking().
      Carries contact attribution so per-recipient reports, the contact
      timeline and CampaignMessage CLICK status keep working exactly as
      with the legacy root-level RecipientLink URLs.

    Legacy RecipientLink (/token) rows are left untouched: emails already
    delivered keep redirecting through ShortUrlRedirectView forever.
    """
    class LinkType(models.TextChoices):
        SHAREABLE = 'SHAREABLE', 'Shareable / Anonymous'
        RECIPIENT = 'RECIPIENT', 'Recipient'

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name='tracking_links', db_index=True
    )
    link_type = models.CharField(
        max_length=16, choices=LinkType.choices, default=LinkType.SHAREABLE,
        db_index=True,
        help_text="SHAREABLE = owner-generated anonymous link; RECIPIENT = per-contact email link.",
    )
    # Recipient links only (NULL for shareable links).
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, null=True, blank=True,
        related_name='campaign_tracking_links',
        help_text="Recipient this link was generated for (recipient links only).",
    )
    shortened_link = models.ForeignKey(
        ShortenedLink, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='campaign_tracking_links',
        help_text="Groups recipient links by destination URL (recipient links only).",
    )
    name = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Human readable label (e.g. Facebook Ad, Website Banner)"
    )
    tracking_token = models.CharField(max_length=16, unique=True, db_index=True)
    # Creation-time cache of the absolute URL. NEVER emit this value:
    # it may have been minted under another environment's base. The
    # token is the identity; always derive the public URL with
    # build_campaign_short_url(link.tracking_token) instead.
    short_url = models.CharField(max_length=500)
    # Optional per-link override. When blank, falls back to
    # Campaign.destination_url at redirect time.
    destination_url = models.TextField(
        blank=True, default='',
        help_text="Destination URL (http:// or https://). Blank = use campaign default."
    )
    is_active = models.BooleanField(default=True, db_index=True)
    click_count = models.PositiveIntegerField(default=0)
    human_click_count = models.PositiveIntegerField(default=0)
    bot_click_count = models.PositiveIntegerField(default=0)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    last_clicked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'campaign_tracking_links'
        ordering = ['-created_at']
        # One recipient URL per (destination, contact, campaign). Nullable on
        # purpose: shareable links keep all three NULL and never collide
        # (NULLs are distinct in unique constraints on all supported DBs).
        unique_together = [('shortened_link', 'contact', 'campaign')]
        indexes = [
            models.Index(fields=['campaign', 'is_active']),
            models.Index(fields=['campaign', 'link_type']),
        ]

    def __str__(self):
        label = self.name or self.tracking_token
        return f"CampaignTrackingLink {self.tracking_token} [{self.link_type}] ({label}) for campaign #{self.campaign_id}"

    def resolve_destination(self) -> str:
        """Per-link destination wins; otherwise the campaign default."""
        own = (self.destination_url or '').strip()
        if own:
            return own
        return (self.campaign.destination_url or '').strip()


class CampaignLinkClickEvent(models.Model):
    """
    Granular click record for every CampaignTrackingLink visit.
    Shareable-link visits stay anonymous (contact NULL); recipient-link
    visits carry the contact for per-recipient attribution, mirroring
    LinkClickEvent.
    """
    class ClickType(models.TextChoices):
        HUMAN = 'HUMAN', 'Human'
        SUSPECTED_BOT = 'SUSPECTED_BOT', 'Suspected Bot'
        UNKNOWN = 'UNKNOWN', 'Unknown'

    link = models.ForeignKey(
        CampaignTrackingLink, on_delete=models.CASCADE, related_name='click_events'
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tracking_link_click_events'
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tracking_link_click_events',
        help_text="Set for recipient-link visits; NULL for anonymous shareable visits.",
    )
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
        db_table = 'campaign_link_click_events'
        ordering = ['-clicked_at']
        indexes = [
            models.Index(fields=['campaign', 'clicked_at']),
            models.Index(fields=['click_type', 'clicked_at']),
            models.Index(fields=['contact', 'clicked_at']),
        ]

    def __str__(self):
        return f"Click [{self.click_type}] on campaign link {self.link.tracking_token} at {self.clicked_at}"
