import os, sys, django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
from django.test import Client
from apps.accounts.models import User
import re

u = User.objects.filter(is_superuser=True).first()
c = Client()
c.force_login(u)
res = c.get('/templates/')
html = res.content.decode('utf-8')

# Find script tags
scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
print(f"Found {len(scripts)} inline script tags.")

# Check the last script tag where template JS lives
if scripts:
    last_script = scripts[-1]
    print(f"Last script length: {len(last_script)} chars")

# Check template cards
cards = re.findall(r'<div class="template-card[^"]*"([^>]*)>', html)
print(f"Found {len(cards)} template cards.")
for card in cards[:3]:
    print("Card attributes:", card)

# Check buttons in first card
match = re.search(r'previewTemplateById\([^\)]*\)', html)
print("Preview call:", match.group(0) if match else "None")

match_edit = re.search(r'editTemplateById\([^\)]*\)', html)
print("Edit call:", match_edit.group(0) if match_edit else "None")

match_del = re.search(r'deleteTemplateById\([^\)]*\)', html)
print("Delete call:", match_del.group(0) if match_del else "None")
