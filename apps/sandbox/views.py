import uuid
import io
import csv
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from .models import SandboxEmail, MockODKSubmission


class MockODKProjectsView(View):
    def get(self, request):
        return JsonResponse([
            {"id": 1, "name": "Pulse Survey 2026", "description": "National Research Pulse Survey"}
        ], safe=False)


class MockODKFormsView(View):
    def get(self, request, project_id):
        sub_count = MockODKSubmission.objects.filter(project_id=str(project_id)).count()
        return JsonResponse([
            {
                "xmlFormId": "pulse_v1",
                "name": "Pulse_V1",
                "version": "1.0",
                "submissions": sub_count
            }
        ], safe=False)


class MockODKSubmissionsCSVView(View):
    def get(self, request, project_id, form_id):
        subs = MockODKSubmission.objects.filter(project_id=str(project_id), form_id=form_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['instanceID', 'SubmissionDate', 'job_id', 'email'])
        for s in subs:
            writer.writerow([s.submission_id, s.submitted_at.isoformat(), s.job_id, s.respondent_email])

        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{form_id}_submissions.csv"'
        return response


class MockODKSubmitView(View):
    """Interactive endpoint to simulate a survey respondent submitting their survey in ODK Central."""

    def post(self, request):
        job_id = request.POST.get('job_id') or request.GET.get('job_id')
        if not job_id:
            return JsonResponse({'error': 'job_id required'}, status=400)

        email = request.POST.get('email', '')
        sub_id = f"uuid:{uuid.uuid4()}"
        sub = MockODKSubmission.objects.create(
            submission_id=sub_id,
            project_id="1",
            form_id="pulse_v1",
            job_id=job_id.strip(),
            respondent_email=email.strip(),
            data={"job_id": job_id.strip(), "satisfaction": "High", "device": "Mobile"}
        )
        return JsonResponse({
            'status': 'submitted',
            'submission_id': sub.submission_id,
            'job_id': sub.job_id,
            'submitted_at': sub.submitted_at.isoformat()
        })


class SandboxWebmailListView(View):
    """Renders the in-app Webmail sandbox interface."""

    def get(self, request):
        emails = SandboxEmail.objects.all().order_by('-created_at')
        submissions = MockODKSubmission.objects.all().order_by('-submitted_at')
        return render(request, 'sandbox/webmail.html', {
            'emails': emails,
            'submissions': submissions,
        })


@method_decorator(xframe_options_exempt, name='dispatch')
class SandboxEmailDetailView(View):
    """Returns details and HTML iframe content of a specific captured email."""

    def get(self, request, pk):
        email_obj = get_object_or_404(SandboxEmail, pk=pk)
        raw_html = request.GET.get('raw') == '1'
        if raw_html:
            return HttpResponse(email_obj.html_content)
        return JsonResponse({
            'id': str(email_obj.id),
            'to_email': email_obj.to_email,
            'from': f"{email_obj.from_name} <{email_obj.from_email}>",
            'subject': email_obj.subject,
            'html_content': email_obj.html_content,
            'text_content': email_obj.text_content,
            'created_at': email_obj.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        })
