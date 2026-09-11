from django.db import models
from django.conf import settings
from core.security import encrypt_value, decrypt_value


class ODKConnection(models.Model):
    class Status(models.TextChoices):
        UNTESTED = 'UNTESTED', 'Untested'
        CONNECTED = 'CONNECTED', 'Connected'
        FAILED = 'FAILED', 'Failed'

    name = models.CharField(max_length=255, help_text="e.g. IRIS ODK Central or Research Server")
    base_url = models.URLField(help_text="e.g. https://odk.company.com or http://localhost:8000/sandbox/odk")
    username = models.CharField(max_length=255, blank=True)
    password_or_token_encrypted = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_mock_sandbox = models.BooleanField(default=False, help_text="If True, queries internal sandbox submission table")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNTESTED)
    last_connection_test = models.DateTimeField(null=True, blank=True)
    last_test_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.base_url})"

    @property
    def password_or_token(self) -> str:
        return decrypt_value(self.password_or_token_encrypted)

    @password_or_token.setter
    def password_or_token(self, raw: str):
        self.password_or_token_encrypted = encrypt_value(raw) if raw else ""


class ODKProject(models.Model):
    connection = models.ForeignKey(ODKConnection, on_delete=models.CASCADE, related_name='projects')
    odk_id = models.IntegerField()
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('connection', 'odk_id')

    def __str__(self):
        return f"{self.name} (Project ID: {self.odk_id})"


class ODKForm(models.Model):
    project = models.ForeignKey(ODKProject, on_delete=models.CASCADE, related_name='forms')
    odk_xml_form_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    version = models.CharField(max_length=100, blank=True)
    submissions_count = models.PositiveIntegerField(default=0)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_submission_timestamp = models.DateTimeField(null=True, blank=True)
    last_submission_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('project', 'odk_xml_form_id')

    def __str__(self):
        return f"{self.name} [{self.odk_xml_form_id}]"


class ODKFieldMapping(models.Model):
    form = models.OneToOneField(ODKForm, on_delete=models.CASCADE, related_name='field_mapping')
    job_id_field = models.CharField(max_length=100, default='job_id', help_text="ODK schema field that holds Contact JobID")
    email_field = models.CharField(max_length=100, blank=True, default='email')
    phone_field = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Mapping for {self.form.name}: Contact.JobID <-> ODK.{self.job_id_field}"


class ODKSyncJob(models.Model):
    class TriggerSource(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual Click'
        SCHEDULED = 'SCHEDULED', 'Scheduled Interval'
        PRE_REMINDER = 'PRE_REMINDER', 'Pre-Reminder Check'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    connection = models.ForeignKey(ODKConnection, on_delete=models.CASCADE, related_name='sync_jobs')
    form = models.ForeignKey(ODKForm, on_delete=models.CASCADE, related_name='sync_jobs')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    trigger_source = models.CharField(max_length=30, choices=TriggerSource.choices, default=TriggerSource.MANUAL)
    
    # Section 22 stats: Submissions Checked, New Submissions, Contacts Marked Used, Unmatched JobIDs, Sync Duration
    submissions_checked = models.PositiveIntegerField(default=0)
    new_submissions = models.PositiveIntegerField(default=0)
    contacts_marked_used = models.PositiveIntegerField(default=0)
    unmatched_job_ids = models.PositiveIntegerField(default=0)
    duration_seconds = models.FloatField(default=0.0)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"ODK Sync #{self.id} on {self.form.name}: Marked {self.contacts_marked_used} Used"


class ODKUnmatchedSubmission(models.Model):
    sync_job = models.ForeignKey(ODKSyncJob, on_delete=models.CASCADE, related_name='unmatched_submissions')
    submission_id = models.CharField(max_length=255)
    job_id_value = models.CharField(max_length=100, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=255)
    raw_data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Unmatched: {self.submission_id} (JobID: '{self.job_id_value}') - {self.reason}"


class ODKDataset(models.Model):
    """Represents an Entity List (Dataset) in ODK Central."""
    project = models.ForeignKey(ODKProject, on_delete=models.CASCADE, related_name='datasets')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    entities_count = models.PositiveIntegerField(default=0)
    unused_count = models.PositiveIntegerField(default=0)
    used_count = models.PositiveIntegerField(default=0)
    last_entity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'name')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.entities_count} entities: {self.unused_count} unused, {self.used_count} used) [{self.project.name}]"


class ODKEntity(models.Model):
    """Represents a single Entity record within an ODK Central Dataset/Entity List."""
    dataset = models.ForeignKey(ODKDataset, on_delete=models.CASCADE, related_name='entities')
    uuid = models.CharField(max_length=255, db_index=True)
    label = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, default='UNUSED', db_index=True)  # UNUSED or USED
    version = models.IntegerField(default=1)
    data = models.JSONField(default=dict, blank=True)
    server_created_at = models.DateTimeField(null=True, blank=True)
    server_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('dataset', 'uuid')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.label or self.uuid} [{self.status}] [{self.dataset.name}]"


