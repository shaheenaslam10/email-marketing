import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from apps.odk.models import ODKConnection, ODKProject, ODKDataset, ODKEntity
from apps.groups.models import ContactGroup, GroupCustomField, GroupContactValue
from apps.contacts.models import Contact
from django.test import Client
from django.contrib.auth import get_user_model

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()

client = Client()
client.force_login(admin_user)

conn, _ = ODKConnection.objects.get_or_create(
    base_url='https://odk.example.com',
    defaults={'name': 'Test ODK', 'username': 'test@example.com', 'is_active': True}
)
proj, _ = ODKProject.objects.get_or_create(
    connection=conn,
    odk_id=999,
    defaults={'name': 'Test Project'}
)
ds, _ = ODKDataset.objects.get_or_create(
    project=proj,
    name='pulse_survey_data',
    defaults={'description': 'Test dataset with custom attributes'}
)

# Create entities with extra custom attributes
ODKEntity.objects.filter(dataset=ds).delete()
ODKEntity.objects.create(
    dataset=ds,
    uuid='uuid-001',
    label='John Doe <john@example.com>',
    status='UNUSED',
    data={
        'email': 'john@example.com',
        'name': 'John Doe',
        'phone': '+923001234567',
        'district': 'Rawalpindi',
        'survey_url': 'https://odk.example.com/survey/john',
        'crop_type': 'Wheat',
        'farm_size': '15 acres'
    }
)
ODKEntity.objects.create(
    dataset=ds,
    uuid='uuid-002',
    label='Jane Smith <jane@example.com>',
    status='USED',
    data={
        'email': 'jane@example.com',
        'name': 'Jane Smith',
        'phone': '+923007654321',
        'district': 'Islamabad',
        'survey_url': 'https://odk.example.com/survey/jane',
        'crop_type': 'Rice',
        'farm_size': '25 acres'
    }
)

# Clean up any previous test group
ContactGroup.objects.filter(name='ODK_Auto_Group_Test').delete()

# Test import-to-contacts with a new group name
payload = {
    'group_name': 'ODK_Auto_Group_Test',
    'mapping': {
        'email': 'email',
        'name': 'name',
        'phone': 'phone'
    }
}

res = client.post(f'/api/odk/datasets/{ds.id}/import-to-contacts/', data=payload, content_type='application/json')
print(f"Import response status: {res.status_code}, data: {res.json()}")

# Verify group created
grp = ContactGroup.objects.get(name='ODK_Auto_Group_Test')
print(f"Created group: {grp.name}, contacts count: {grp.contacts.count()}")
print(f"Selected fields on group: {grp.selected_fields}")

# Verify custom fields created on group
cfields = list(grp.custom_fields.values('name', 'slug', 'is_active'))
print("Custom fields auto-created on group:")
for cf in cfields:
    print(f"  - {cf['name']} (slug: {cf['slug']}, active: {cf['is_active']})")

# Verify contact values saved
contact_john = Contact.objects.get(email='john@example.com')
john_vals = list(GroupContactValue.objects.filter(contact=contact_john, field__group=grp).values('field__name', 'value'))
print(f"Custom values for John Doe:")
for val in john_vals:
    print(f"  - {val['field__name']}: {val['value']}")

assert len(cfields) >= 3, "Expected custom fields to be auto-created"
assert len(john_vals) >= 3, "Expected custom values to be saved"
print("\n>>> ODK IMPORT CUSTOM FIELDS TEST PASSED! <<<")
