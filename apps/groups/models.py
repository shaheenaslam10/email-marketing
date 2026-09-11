from django.db import models
from django.utils.text import slugify


class ContactGroup(models.Model):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    contacts = models.ManyToManyField('contacts.Contact', related_name='groups', blank=True)
    selected_fields = models.JSONField(default=list, blank=True, help_text="List of active field keys for this group")
    field_mappings = models.JSONField(default=dict, blank=True, help_text="Stored mapping configuration from ODK or file import")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.contacts.count()} contacts)"

    @property
    def contact_count(self) -> int:
        return self.contacts.count()

    @property
    def unused_count(self) -> int:
        return self.contacts.filter(status='UNUSED').count()

    @property
    def used_count(self) -> int:
        return self.contacts.filter(status='USED').count()


class GroupCustomField(models.Model):
    """Group-specific custom fields shown when adding/editing contacts within a group."""

    class FieldType(models.TextChoices):
        TEXT = 'TEXT', 'Text'
        NUMBER = 'NUMBER', 'Number'
        DATE = 'DATE', 'Date'
        BOOLEAN = 'BOOLEAN', 'Boolean (Yes/No)'
        EMAIL = 'EMAIL', 'Email'
        PHONE = 'PHONE', 'Phone'
        URL = 'URL', 'URL'
        SELECT = 'SELECT', 'Dropdown Select'

    group = models.ForeignKey(ContactGroup, on_delete=models.CASCADE, related_name='custom_fields')
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100)
    field_type = models.CharField(max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    description = models.CharField(max_length=255, blank=True)
    options = models.JSONField(default=list, blank=True, help_text="List of options for SELECT type fields")
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(
        default=True,
        help_text="Active fields are shown as columns in the contacts table and in add/edit modals"
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'name']
        unique_together = ('group', 'slug')

    def __str__(self):
        return f"{self.group.name} → {self.name} ({self.field_type})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            # Make slug unique within the group
            slug = base_slug
            n = 1
            while GroupCustomField.objects.filter(group=self.group, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


class GroupContactValue(models.Model):
    """Stores values for group-specific custom fields per contact."""
    contact = models.ForeignKey(
        'contacts.Contact',
        on_delete=models.CASCADE,
        related_name='group_custom_values'
    )
    field = models.ForeignKey(GroupCustomField, on_delete=models.CASCADE, related_name='values')
    value = models.TextField(blank=True)

    class Meta:
        unique_together = ('contact', 'field')

    def __str__(self):
        return f"{self.contact.email} | {self.field.group.name}.{self.field.name}: {self.value}"


class TestEmailGroup(models.Model):
    """
    Multi-Email Testing Groups for previewing and test dispatching campaigns.
    Allows QA, review, and staging teams to receive test emails in bulk.
    """
    name = models.CharField(max_length=255, unique=True, help_text="e.g. Core QA Team, Executive Review")
    description = models.CharField(max_length=255, blank=True)
    emails = models.TextField(help_text="Comma, semicolon, or newline-separated list of test email addresses")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({len(self.email_list)} recipients)"

    @property
    def email_list(self) -> list:
        if not self.emails:
            return []
        import re
        raw = re.split(r'[,;\s\n\r\t]+', self.emails)
        cleaned = [e.strip() for e in raw if e and '@' in e]
        seen = set()
        result = []
        for e in cleaned:
            low = e.lower()
            if low not in seen:
                seen.add(low)
                result.append(e)
        return result

    @property
    def recipient_count(self) -> int:
        return len(self.email_list)

