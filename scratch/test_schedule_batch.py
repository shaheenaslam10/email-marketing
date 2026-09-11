import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact, ContactGroup
from apps.reminders.services import launch_initial_campaign, execute_reminder_cycle
from django.utils import timezone
from datetime import timedelta

print("Checking Campaign model fields...")
c = Campaign.objects.first()
if c:
    print(f"Campaign found: {c.name} (status: {c.status})")
    print(f"send_mode: {c.send_mode}, batch_size: {c.batch_size}, batch_interval_minutes: {c.batch_interval_minutes}")

    # Test updating batch settings
    c.batch_size = 30
    c.batch_interval_minutes = 20
    c.send_mode = Campaign.SendMode.BATCHES
    c.save(update_fields=['batch_size', 'batch_interval_minutes', 'send_mode'])
    c.refresh_from_db()
    assert c.batch_size == 30
    assert c.batch_interval_minutes == 20
    assert c.send_mode == 'BATCHES'
    print("Campaign batch configuration verified successfully!")
else:
    print("No campaign found, creating test campaign...")
    grp, _ = ContactGroup.objects.get_or_create(name="Test Group")
    c = Campaign.objects.create(
        name="Test Schedule Campaign",
        subject="Hello Survey",
        html_content="<p>Test survey {{Survey_Link}}</p>",
        send_mode=Campaign.SendMode.BATCHES,
        batch_size=25,
        batch_interval_minutes=15
    )
    c.groups.add(grp)
    print(f"Created test campaign {c.id}")

print("All verifications passed.")
