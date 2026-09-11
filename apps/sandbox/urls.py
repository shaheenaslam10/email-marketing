from django.urls import path
from .views import (
    MockODKProjectsView, MockODKFormsView, MockODKSubmissionsCSVView,
    MockODKSubmitView, SandboxWebmailListView, SandboxEmailDetailView
)

urlpatterns = [
    # Mock ODK Central REST endpoints
    path('sandbox/api/odk/v1/projects', MockODKProjectsView.as_view(), name='mock_odk_projects'),
    path('sandbox/api/odk/v1/projects/<int:project_id>/forms', MockODKFormsView.as_view(), name='mock_odk_forms'),
    path('sandbox/api/odk/v1/projects/<int:project_id>/forms/<str:form_id>/submissions.csv', MockODKSubmissionsCSVView.as_view(), name='mock_odk_submissions_csv'),
    path('sandbox/api/odk/submit/', MockODKSubmitView.as_view(), name='mock_odk_submit'),

    # In-App Sandbox Webmail UI
    path('sandbox/webmail/', SandboxWebmailListView.as_view(), name='sandbox_webmail'),
    path('sandbox/webmail/<uuid:pk>/', SandboxEmailDetailView.as_view(), name='sandbox_email_detail'),
]
