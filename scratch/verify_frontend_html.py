import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()

client = Client()
client.force_login(admin_user)

print("=== 1. VERIFY CUSTOM FIELDS PAGE (/contacts/custom-fields/) ===")
res = client.get('/contacts/custom-fields/')
assert res.status_code == 200, f"Expected 200 but got {res.status_code}"
content = res.content.decode('utf-8')
assert 'group-select' in content, "Missing group selector dropdown"
assert 'stat-total-fields' in content, "Missing total fields stat"
assert 'stat-active-fields' in content, "Missing active fields stat"
assert 'btn-add-group-field' in content, "Missing add group field button"
assert 'toggleFieldActive' in content, "Missing toggleFieldActive function"
print("OK: Custom Fields page contains group-centric management UI, group switcher, and active toggle logic.")

print("\n=== 2. VERIFY CAMPAIGNS PAGE (/campaigns/) ===")
res = client.get('/campaigns/')
assert res.status_code == 200, f"Expected 200 but got {res.status_code}"
content = res.content.decode('utf-8')
assert 'selectDuplicateCampaigns' in content, "Missing selectDuplicateCampaigns button"
assert 'campaigns-bulk-bar' in content, "Missing campaigns bulk action bar"
assert 'openRenameModal' in content, "Missing rename campaign modal trigger"
assert 'deleteSingleCampaign' in content, "Missing delete campaign function"
assert 'executeBulkDeleteCampaigns' in content, "Missing executeBulkDeleteCampaigns function"
print("OK: Campaigns page contains Select Duplicates, Bulk Delete bar, Rename modal, and Delete actions.")

print("\n=== 3. VERIFY SENDERS PAGE (/settings/senders/) ===")
res = client.get('/settings/senders/')
assert res.status_code == 200, f"Expected 200 but got {res.status_code}"
content = res.content.decode('utf-8')
assert 'deleteSender' in content, "Missing deleteSender function"
print("OK: Senders page contains Delete Sender option.")

print("\n=== 4. VERIFY GLOBAL PAGINATION OPTIONS IN BASE TEMPLATE ===")
res = client.get('/contacts/')
content = res.content.decode('utf-8')
assert 'value="200"' in content, "Missing 200 in pagination"
assert 'value="400"' in content, "Missing 400 in pagination"
assert 'value="600"' in content, "Missing 600 in pagination"
assert 'value="1000"' in content, "Missing 1000 in pagination"
assert 'value="99999"' in content, "Missing All (99999) in pagination"
print("OK: Global pagination in base.html has 200, 400, 600, 1000, and All.")

print("\n>>> ALL FRONTEND HTML AND TEMPLATE CHECKS PASSED! <<<")
