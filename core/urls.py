from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from apps.accounts.views import LoginView, LogoutView
from core import ui_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Authentication
    path('login/', LoginView.as_view(), name='login'),
    path('accounts/login/', LoginView.as_view(), name='accounts_login'),
    path('logout/', LogoutView.as_view(), name='logout'),

    # Brevo-Style Web Interface
    path('', ui_views.dashboard_view, name='dashboard'),
    path('contacts/', ui_views.contacts_view, name='contacts'),
    path('contacts/groups/', ui_views.groups_view, name='contact_groups'),
    path('contacts/import/', ui_views.import_wizard_view, name='contact_import'),
    path('contacts/custom-fields/', ui_views.custom_fields_view, name='custom_fields'),

    path('campaigns/', ui_views.campaigns_view, name='campaigns'),
    path('campaigns/create/', ui_views.campaign_wizard_view, name='campaign_create'),
    path('campaigns/<int:pk>/edit/', ui_views.campaign_wizard_view, name='campaign_edit'),
    path('campaigns/<int:pk>/report/', ui_views.campaign_report_view, name='campaign_report'),
    path('templates/', ui_views.templates_view, name='email_templates'),

    path('odk/', ui_views.odk_view, name='odk_central'),
    path('odk/unmatched/', ui_views.odk_unmatched_view, name='odk_unmatched'),

    path('settings/senders/', ui_views.senders_view, name='settings_senders'),
    path('settings/audit/', ui_views.audit_view, name='settings_audit'),
    path('settings/users/', ui_views.users_view, name='settings_users'),

    # REST APIs
    path('api/', include('apps.accounts.urls')),
    path('api/', include('apps.contacts.urls')),
    path('api/', include('apps.groups.urls')),
    path('api/imports/', include('apps.imports.urls')),
    path('api/', include('apps.senders.urls')),
    path('api/', include('apps.email_templates.urls')),
    path('api/', include('apps.campaigns.urls')),
    path('api/', include('apps.reminders.urls')),
    path('api/odk/', include('apps.odk.urls')),
    path('api/', include('apps.reports.urls')),
    path('api/', include('apps.audit.urls')),
    path('api/', include('apps.settings_app.urls')),
    path('api/', include('apps.webhooks.urls')),

    # Interactive Mock Sandbox & In-App Webmail
    path('', include('apps.sandbox.urls')),

    # Tracking & Pixel Links (Section 73, 75, 76, URL Shortener)
    path('', include('apps.tracking.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
