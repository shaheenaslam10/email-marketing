from django.db import models
from django.conf import settings
from core.security import encrypt_value, decrypt_value


class Contact(models.Model):
    class UsageStatus(models.TextChoices):
        UNUSED = 'UNUSED', 'Unused'
        USED = 'USED', 'Used'

    class EmailStatus(models.TextChoices):
        ACTIVE = 'Active', 'Active'
        INVALID = 'Invalid', 'Invalid'
        UNSUBSCRIBED = 'Unsubscribed', 'Unsubscribed'
        SOFT_BOUNCE = 'Soft Bounce', 'Soft Bounce'
        HARD_BOUNCE = 'Hard Bounce', 'Hard Bounce'
        BLOCKED = 'Blocked', 'Blocked'

    name = models.CharField(max_length=255)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(db_index=True)
    phone_number = models.CharField(max_length=50, blank=True)

    # Sensitive external survey login credential encrypted at rest
    login_password_encrypted = models.TextField(blank=True)

    # Primary survey identifier matching ODK submission records
    job_id = models.CharField(max_length=500, blank=True, default='', db_index=True)

    # Survey completion status
    status = models.CharField(
        max_length=20,
        choices=UsageStatus.choices,
        default=UsageStatus.UNUSED,
        db_index=True
    )

    # Separate delivery/reputation status
    email_status = models.CharField(
        max_length=30,
        choices=EmailStatus.choices,
        default=EmailStatus.ACTIVE,
        db_index=True
    )

    # ODK Central integration fields
    odk_submission_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    odk_submitted_at = models.DateTimeField(blank=True, null=True)
    odk_last_checked_at = models.DateTimeField(blank=True, null=True)
    status_source = models.CharField(max_length=50, default='MANUAL')  # ODK_CENTRAL, MANUAL, IMPORT

    # Sending metrics
    last_email_sent_at = models.DateTimeField(blank=True, null=True)
    last_reminder_sent_at = models.DateTimeField(blank=True, null=True)
    reminder_count = models.PositiveIntegerField(default=0)

    unsubscribed = models.BooleanField(default=False, db_index=True)
    bounce_status = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['job_id']),
            models.Index(fields=['status']),
            models.Index(fields=['email_status']),
            models.Index(fields=['unsubscribed']),
        ]

    def __str__(self):
        return f"{self.name} ({self.email}) - [{self.job_id}] {self.status}"

    @property
    def login_password(self) -> str:
        """Decrypts and returns the sensitive survey respondent password in memory."""
        return decrypt_value(self.login_password_encrypted)

    @login_password.setter
    def login_password(self, plain_text: str):
        """Encrypts survey respondent password before saving to the database."""
        self.login_password_encrypted = encrypt_value(plain_text) if plain_text else ""

    @property
    def is_eligible_for_reminder(self) -> bool:
        """Business rule: Must be UNUSED, not unsubscribed, and active email."""
        return (
            self.status == self.UsageStatus.UNUSED and
            not self.unsubscribed and
            self.email_status == self.EmailStatus.ACTIVE
        )


class ContactStatusHistory(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='status_history')
    old_status = models.CharField(max_length=20)
    new_status = models.CharField(max_length=20)
    changed_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=50)  # ODK_CENTRAL, MANUAL, IMPORT, SYSTEM
    odk_submission_id = models.CharField(max_length=255, blank=True, null=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-changed_at']

    def __str__(self):
        return f"{self.contact.email}: {self.old_status} -> {self.new_status} at {self.changed_at}"


class ContactCustomField(models.Model):
    class FieldType(models.TextChoices):
        TEXT = 'TEXT', 'Text'
        NUMBER = 'NUMBER', 'Number'
        DATE = 'DATE', 'Date'
        BOOLEAN = 'BOOLEAN', 'Boolean'
        EMAIL = 'EMAIL', 'Email'
        PHONE = 'PHONE', 'Phone'
        URL = 'URL', 'URL'

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    field_type = models.CharField(max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({{{{{self.slug}}}}})"


class ContactCustomValue(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='custom_values')
    field = models.ForeignKey(ContactCustomField, on_delete=models.CASCADE, related_name='values')
    value = models.TextField(blank=True)

    class Meta:
        unique_together = ('contact', 'field')

    def __str__(self):
        return f"{self.contact.email} - {self.field.name}: {self.value}"
