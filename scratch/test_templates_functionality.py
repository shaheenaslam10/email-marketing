import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.test import Client
from apps.accounts.models import User
from apps.email_templates.models import EmailTemplate

# 1. Login user
user = User.objects.filter(is_superuser=True).first()
client = Client()
client.force_login(user)

# 2. Test GET /templates/ page
res_page = client.get('/templates/')
assert res_page.status_code == 200, f"Expected 200, got {res_page.status_code}"
content = res_page.content.decode('utf-8')
assert 'id="template-sort-select"' in content, "Sort select missing from HTML"
assert 'id="template-category-filter"' in content, "Category filter missing from HTML"
assert 'id="select-all-templates-cb"' in content, "Select all checkbox missing from HTML"
assert 'id="bulk-action-bar"' in content, "Bulk action bar missing from HTML"
assert 'previewTemplateById' in content, "previewTemplateById missing from scripts"
assert 'editTemplateById' in content, "editTemplateById missing from scripts"
assert 'deleteTemplateById' in content, "deleteTemplateById missing from scripts"
assert 'confirmBulkDeleteTemplates' in content, "confirmBulkDeleteTemplates missing from scripts"
print("[PASS] /templates/ HTML page test passed!")

# 3. Create test templates for API testing
t1 = EmailTemplate.objects.create(name='Test Template Alpha', category='Survey Invitation', subject='Subject Alpha', html_content='<p>Alpha</p>')
t2 = EmailTemplate.objects.create(name='Test Template Beta', category='Follow-up Reminder', subject='Subject Beta', html_content='<p>Beta</p>')
print(f"[PASS] Created test templates: ID {t1.id}, ID {t2.id}")

# 4. Test GET /api/templates/<id>/ (View / Edit fetch)
res_get = client.get(f'/api/templates/{t1.id}/')
assert res_get.status_code == 200, f"Expected 200 on GET, got {res_get.status_code}"
data = res_get.json()
assert data['name'] == 'Test Template Alpha'
print(f"[PASS] GET /api/templates/{t1.id}/ passed!")

# 5. Test PUT /api/templates/<id>/ (Edit update)
res_put = client.put(f'/api/templates/{t1.id}/', data={
    'name': 'Test Template Alpha Updated',
    'category': 'Survey Invitation',
    'subject': 'Subject Alpha Updated',
    'preview_text': 'Snippet',
    'html_content': '<p>Updated content</p>',
    'text_content': 'Updated content'
}, content_type='application/json')
assert res_put.status_code == 200, f"Expected 200 on PUT, got {res_put.status_code}"
t1.refresh_from_db()
assert t1.name == 'Test Template Alpha Updated'
print(f"[PASS] PUT /api/templates/{t1.id}/ passed!")

# 6. Test DELETE /api/templates/<id>/ (Single delete)
res_del = client.delete(f'/api/templates/{t1.id}/')
assert res_del.status_code == 204, f"Expected 204 on DELETE, got {res_del.status_code}"
assert not EmailTemplate.objects.filter(id=t1.id).exists()
print(f"[PASS] DELETE /api/templates/{t1.id}/ passed!")

# 7. Test POST /api/templates/bulk-delete/ (Multiselect bulk delete)
t3 = EmailTemplate.objects.create(name='Test Template Gamma', category='General', subject='Subject Gamma', html_content='<p>Gamma</p>')
res_bulk = client.post('/api/templates/bulk-delete/', data={'ids': [t2.id, t3.id]}, content_type='application/json')
assert res_bulk.status_code == 200, f"Expected 200 on bulk delete, got {res_bulk.status_code}"
bulk_data = res_bulk.json()
assert bulk_data.get('status') == 'success'
assert not EmailTemplate.objects.filter(id__in=[t2.id, t3.id]).exists()
print("[PASS] POST /api/templates/bulk-delete/ passed!")

print("\nALL VERIFICATION TESTS COMPLETED SUCCESSFULLY!")
