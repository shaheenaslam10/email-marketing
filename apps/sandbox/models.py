import uuid
from django.db import models


class SandboxEmail(models.Model):
    """Stores outgoing emails in Sandbox mode for local inspection and webmail preview."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    to_email = models.EmailField()
    from_name = models.CharField(max_length=255, blank=True)
    from_email = models.EmailField()
    reply_to = models.EmailField(blank=True)
    subject = models.CharField(max_length=500)
    html_content = models.TextField()
    text_content = models.TextField(blank=True)
    headers = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    provider_type = models.CharField(max_length=50, default='SANDBOX')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at.strftime('%H:%M:%S')}] To: {self.to_email} - {self.subject}"


class MockODKSubmission(models.Model):
    """Simulates an ODK Central submission record in the local Sandbox."""
    submission_id = models.CharField(max_length=255, unique=True)
    project_id = models.CharField(max_length=100, default='1')
    form_id = models.CharField(max_length=100, default='pulse_v1')
    job_id = models.CharField(max_length=100, db_index=True)
    respondent_email = models.EmailField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"ODK Submission: {self.submission_id} (JobID: {self.job_id})"
