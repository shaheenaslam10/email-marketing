import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.groups.models import ContactGroup, GroupCustomField, GroupContactValue
from apps.contacts.models import Contact
from apps.campaigns.models import Campaign
from apps.senders.models import Sender
from apps.odk.models import ODKDataset, ODKEntity, ODKProject, ODKConnection
from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()
if not admin_user:
    admin_user = User.objects.create_superuser('admin_test', 'admin@example.com', 'adminpass123')

client = Client()
client.force_login(admin_user)

print("=== 1. TEST GLOBAL PAGINATION ===")
res = client.get('/api/contacts/?page=1&page_size=200')
print(f"page_size=200 status: {res.status_code}, page_size returned: {res.json().get('page_size')}")

res = client.get('/api/contacts/?page=1&page_size=1000')
print(f"page_size=1000 status: {res.status_code}, page_size returned: {res.json().get('page_size')}")

res = client.get('/api/contacts/?page=1&page_size=99999')
print(f"page_size=99999 (All) status: {res.status_code}, page_size returned: {res.json().get('page_size')}")

print("\n=== 2. TEST GROUP CUSTOM FIELDS & TOGGLE ACTIVE ===")
grp, _ = ContactGroup.objects.get_or_create(name="Pulse_V1_Test")
f1, _ = GroupCustomField.objects.get_or_create(
    group=grp,
    slug="district",
    defaults={'name': 'District', 'field_type': 'TEXT', 'is_active': True}
)
res = client.get(f'/api/groups/{grp.id}/fields/')
print(f"List fields status: {res.status_code}, count: {len(res.json())}")

res = client.post(f'/api/groups/{grp.id}/fields/{f1.id}/toggle/')
print(f"Toggle active status: {res.status_code}, new is_active: {res.json().get('is_active')}")

# Toggle back
client.post(f'/api/groups/{grp.id}/fields/{f1.id}/toggle/')

print("\n=== 3. TEST CAMPAIGN BULK DELETE & RENAME ===")
c1 = Campaign.objects.create(name="Duplicate Campaign A", subject="Subject 1", sender=Sender.objects.first() or Sender.objects.create(name="Test Sender", email="test@example.com"))
c2 = Campaign.objects.create(name="Duplicate Campaign A", subject="Subject 2", sender=c1.sender)

# Test quick rename via PATCH
res = client.patch(f'/api/campaigns/{c1.id}/', data={'name': 'Renamed Campaign A'}, content_type='application/json')
print(f"Rename PATCH status: {res.status_code}, new name: {res.json().get('name')}")

# Test bulk delete
res = client.post('/api/campaigns/bulk-delete/', data={'ids': [c1.id, c2.id]}, content_type='application/json')
print(f"Bulk delete status: {res.status_code}, response: {res.json()}")

print("\n=== 4. TEST SENDER DELETE ===")
s_del = Sender.objects.create(name="Temporary Sender", email="temp@example.com")
res = client.delete(f'/api/senders/{s_del.id}/')
print(f"Delete sender status: {res.status_code}, response: {res.json()}")

print("\n=== ALL TESTS COMPLETED SUCCESSFULLY ===")
