from django.db import models


class EmailTemplate(models.Model):
    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=500, blank=True)
    preview_text = models.CharField(max_length=255, blank=True)
    html_content = models.TextField()
    text_content = models.TextField(blank=True)
    category = models.CharField(max_length=100, default='Survey Invitation', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def save(self, *args, **kwargs):
        if self.category:
            self.category = self.category.strip("'\" \t\r\n")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

