from typing import Tuple
from django.db import models
from django.utils import timezone
from core.security import encrypt_value, decrypt_value


class Sender(models.Model):
    class ProviderType(models.TextChoices):
        SANDBOX = 'SANDBOX', 'Internal Sandbox / Webmail Simulator'
        SMTP = 'SMTP', 'Custom SMTP'
        BREVO = 'BREVO', 'Brevo (Sendinblue) API'
        AMAZON_SES = 'AMAZON_SES', 'Amazon SES API'
        MAILGUN = 'MAILGUN', 'Mailgun API'
        SENDGRID = 'SENDGRID', 'SendGrid API'
        POSTMARK = 'POSTMARK', 'Postmark API'

    name = models.CharField(max_length=255, help_text="Friendly sender name e.g. IRIS Communications")
    email = models.EmailField(help_text="Sender email address e.g. survey@company.com")
    reply_to = models.EmailField(blank=True, help_text="Optional reply-to address")

    provider_type = models.CharField(
        max_length=30,
        choices=ProviderType.choices,
        default=ProviderType.SANDBOX
    )

    # SMTP / API credentials (encrypted at rest)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=587)
    username = models.CharField(max_length=255, blank=True)
    password_or_key_encrypted = models.TextField(blank=True)
    use_tls = models.BooleanField(default=True)
    use_ssl = models.BooleanField(default=False)

    # API specific options (domain, region, etc.)
    api_domain = models.CharField(max_length=255, blank=True, help_text="For Mailgun domain or Brevo endpoint")
    api_region = models.CharField(max_length=50, blank=True, default='us-east-1', help_text="For AWS SES region")

    is_active = models.BooleanField(default=True)

    # When True, providers that support a per-message opt-out are asked to
    # skip their own click-link rewriting so recipients see our branded
    # tracking URLs. Our own tracking stays fully active. Currently
    # honored by SendGrid, Mailgun and Postmark. Brevo offers no such
    # mechanism for transactional mail: its link rewriting can only be
    # addressed at the Brevo account level (support ticket), never via API.
    disable_provider_click_tracking = models.BooleanField(
        default=False,
        help_text="Ask the provider to skip its own click-link rewriting (SendGrid/Mailgun/Postmark). Our tracking stays active. No effect on Brevo, which offers no API opt-out."
    )

    # Rate limiting (Section 25 & 52)
    daily_limit = models.PositiveIntegerField(default=80000)
    hourly_limit = models.PositiveIntegerField(default=3000)
    per_minute_limit = models.PositiveIntegerField(default=50)

    # Usage counters
    today_sent = models.PositiveIntegerField(default=0)
    this_hour_sent = models.PositiveIntegerField(default=0)
    this_minute_sent = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.get_provider_type_display()})"

    @property
    def display_from(self) -> str:
        return f"{self.name} <{self.email}>"

    @property
    def password_or_key(self) -> str:
        return decrypt_value(self.password_or_key_encrypted)

    @password_or_key.setter
    def password_or_key(self, raw_value: str):
        self.password_or_key_encrypted = encrypt_value(raw_value) if raw_value else ""

    def check_rate_limits(self) -> Tuple[bool, str]:
        """Validates current sending velocity against configured thresholds."""
        now = timezone.now()
        # Reset counters if hour/day changed
        if self.last_sent_at:
            if self.last_sent_at.date() != now.date():
                self.today_sent = 0
                self.this_hour_sent = 0
                self.this_minute_sent = 0
            elif self.last_sent_at.hour != now.hour:
                self.this_hour_sent = 0
                self.this_minute_sent = 0
            elif (now - self.last_sent_at).total_seconds() > 60:
                self.this_minute_sent = 0

        if self.daily_limit > 0 and self.today_sent >= self.daily_limit:
            return False, f"Daily limit of {self.daily_limit} reached."
        if self.hourly_limit > 0 and self.this_hour_sent >= self.hourly_limit:
            return False, f"Hourly limit of {self.hourly_limit} reached."
        if self.per_minute_limit > 0 and self.this_minute_sent >= self.per_minute_limit:
            return False, f"Per-minute limit of {self.per_minute_limit} reached."

        return True, "OK"

    def record_send(self):
        """Increments send rate counters."""
        now = timezone.now()
        self.today_sent += 1
        self.this_hour_sent += 1
        self.this_minute_sent += 1
        self.last_sent_at = now
        self.save(update_fields=['today_sent', 'this_hour_sent', 'this_minute_sent', 'last_sent_at'])
