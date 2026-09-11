from django.db import models
from django.conf import settings
from apps.groups.models import ContactGroup


class ImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ANALYZED = 'ANALYZED', 'Analyzed'
        PROCESSING = 'PROCESSING', 'Processing'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    class DuplicateStrategy(models.TextChoices):
        SKIP = 'SKIP', 'Skip duplicates'
        UPDATE = 'UPDATE', 'Update existing contacts'
        ADD_TO_GROUP = 'ADD_TO_GROUP', 'Add existing contacts to group'

    file = models.FileField(upload_to='imports/%Y/%m/%d/')
    file_type = models.CharField(max_length=10, default='CSV')
    field_mapping = models.JSONField(default=dict, blank=True)
    duplicate_strategy = models.CharField(
        max_length=20,
        choices=DuplicateStrategy.choices,
        default=DuplicateStrategy.UPDATE
    )
    target_group = models.ForeignKey(ContactGroup, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Validation & Analytics Counters
    total_rows = models.PositiveIntegerField(default=0)
    valid_contacts = models.PositiveIntegerField(default=0)
    new_contacts = models.PositiveIntegerField(default=0)
    existing_contacts = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    invalid_emails = models.PositiveIntegerField(default=0)
    missing_emails = models.PositiveIntegerField(default=0)
    missing_job_ids = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Import #{self.id} ({self.status}) - {self.total_rows} rows"
