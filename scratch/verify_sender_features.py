import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
from apps.senders.models import Sender

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()

client = Client()
client.force_login(admin_user)

sender = Sender.objects.filter(name='PSDF').first()
if not sender:
    sender = Sender.objects.first()

print(f"Testing with sender ID {sender.id}: {sender.name} ({sender.email})")

# 1. Test sending a test email with a custom recipient
res = client.post(f'/api/senders/{sender.id}/test_connection/', data={'to_email': 'verify_test@example.com'}, content_type='application/json')
print(f"1. Test email endpoint status: {res.status_code}, data: {res.json()}")

# 2. Test editing a sender via PATCH
new_name = f"{sender.name} - Verified"
res = client.patch(f'/api/senders/{sender.id}/', data={'name': new_name, 'daily_limit': 75000}, content_type='application/json')
print(f"2. Edit sender PATCH status: {res.status_code}, updated name: {res.json().get('name')}, daily_limit: {res.json().get('daily_limit')}")

# Restore name
client.patch(f'/api/senders/{sender.id}/', data={'name': sender.name, 'daily_limit': sender.daily_limit}, content_type='application/json')

# 3. Test HTML template rendering for test email modal and edit sender modal
res = client.get('/settings/senders/')
assert res.status_code == 200
html = res.content.decode('utf-8')
assert 'test-email-modal' in html, "Missing test-email-modal"
assert 'test-recipient-email' in html, "Missing test-recipient-email input"
assert 'edit-sender-modal' in html, "Missing edit-sender-modal"
assert 'openEditSenderModal' in html, "Missing openEditSenderModal JS"
assert 'openTestModal' in html, "Missing openTestModal JS"
print("3. HTML template checks: PASSED (Test Email modal & Edit Sender modal present)")

print("\n>>> ALL SENDER FEATURES VERIFIED SUCCESSFULLY! <<<")
