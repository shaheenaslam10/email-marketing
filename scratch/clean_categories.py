import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from apps.email_templates.models import EmailTemplate

updated = 0
for t in EmailTemplate.objects.all():
    cleaned = t.category.strip('\'" \t\r\n') if t.category else 'General'
    if cleaned != t.category:
        t.category = cleaned
        t.save()
        updated += 1
print(f"Cleaned {updated} templates.")
print("Current categories in DB:", list(EmailTemplate.objects.values_list('category', flat=True).distinct()))
