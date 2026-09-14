import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import ensure_csrf_cookie
from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup, GroupCustomField
from apps.odk.models import ODKConnection, ODKForm, ODKDataset
from apps.senders.models import Sender
from apps.email_templates.models import EmailTemplate
from apps.email_templates.serializers import EmailTemplateSerializer
from apps.audit.models import AuditLog
from apps.tracking.utils import get_shortener_base_url


@login_required
def dashboard_view(request):
    return render(request, 'dashboard/index.html')


@login_required
def contacts_view(request):
    groups = ContactGroup.objects.all()
    return render(request, 'contacts/index.html', {'groups': groups})


@login_required
def groups_view(request):
    return render(request, 'contacts/groups.html')


@login_required
def import_wizard_view(request):
    groups = ContactGroup.objects.all()
    return render(request, 'contacts/import.html', {'groups': groups})


@login_required
def custom_fields_view(request):
    return render(request, 'contacts/custom_fields.html')


@login_required
def campaigns_view(request):
    return render(request, 'campaigns/index.html')


@login_required
def campaign_wizard_view(request, pk=None):
    campaign = get_object_or_404(Campaign, pk=pk) if pk else None
    senders = Sender.objects.filter(is_active=True)
    groups = ContactGroup.objects.all()
    forms = ODKForm.objects.select_related('project', 'project__connection').all()
    datasets = ODKDataset.objects.select_related('project', 'project__connection').all().order_by('project__name', 'name')
    templates = EmailTemplate.objects.all()
    selected_group_ids = list(campaign.groups.values_list('id', flat=True)) if campaign else []
    group_custom_fields = GroupCustomField.objects.filter(is_active=True).values('name', 'slug', 'field_type').distinct().order_by('name')
    template_id = request.GET.get('template_id')
    selected_template = EmailTemplate.objects.filter(id=template_id).first() if template_id else None
    templates_json = json.dumps(list(EmailTemplateSerializer(templates, many=True).data))

    return render(request, 'campaigns/wizard.html', {
        'campaign': campaign,
        'senders': senders,
        'groups': groups,
        'selected_group_ids': selected_group_ids,
        'group_custom_fields': group_custom_fields,
        'selected_template': selected_template,
        'odk_forms': forms,
        'odk_datasets': datasets,
        'templates': templates,
        'templates_json': templates_json,
        'shortener_base_url': get_shortener_base_url(),
    })


@login_required
@ensure_csrf_cookie
def templates_view(request):
    templates = EmailTemplate.objects.all().order_by('-updated_at')
    raw_cats = EmailTemplate.objects.exclude(category__isnull=True).exclude(category='').values_list('category', flat=True).distinct()
    cleaned_cats = sorted(list(set(c.strip("'\" \t\r\n") for c in raw_cats if c and c.strip("'\" \t\r\n"))))
    group_custom_fields = GroupCustomField.objects.filter(is_active=True).values('name', 'slug', 'field_type').distinct().order_by('name')
    return render(request, 'campaigns/templates.html', {
        'templates': templates,
        'categories': cleaned_cats,
        'group_custom_fields': group_custom_fields,
        'shortener_base_url': get_shortener_base_url(),
    })


@login_required
def campaign_report_view(request, pk):
    campaign = get_object_or_404(Campaign, pk=pk)
    return render(request, 'reports/campaign_report.html', {'campaign': campaign})


@login_required
def odk_view(request):
    connections = ODKConnection.objects.all()
    forms = ODKForm.objects.select_related('project', 'project__connection').all()
    return render(request, 'odk/index.html', {'connections': connections, 'forms': forms})


@login_required
def odk_unmatched_view(request):
    return render(request, 'odk/unmatched.html')


@login_required
def senders_view(request):
    return render(request, 'settings/senders.html')


@login_required
def audit_view(request):
    return render(request, 'settings/audit.html')


@login_required
def users_view(request):
    return render(request, 'settings/users.html')
