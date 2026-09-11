from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.senders.models import Sender
from apps.contacts.models import Contact, ContactStatusHistory
from apps.groups.models import ContactGroup
from apps.odk.models import ODKConnection, ODKProject, ODKForm, ODKFieldMapping
from apps.email_templates.models import EmailTemplate

User = get_user_model()


class Command(BaseCommand):
    help = "Seeds initial superuser, sandbox sender, ODK connection, email templates, and sample contacts."

    def handle(self, *args, **options):
        # 1. Super Admin User
        admin_user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@platform.local',
                'first_name': 'Super',
                'last_name': 'Admin',
                'role': User.Role.SUPER_ADMIN,
                'is_staff': True,
                'is_superuser': True
            }
        )
        if created:
            admin_user.set_password('admin123')
            admin_user.save()
            self.stdout.write(self.style.SUCCESS("Created admin user (admin / admin123)"))
        else:
            self.stdout.write("Admin user already exists.")

        # 2. Sandbox Sender
        sender, _ = Sender.objects.get_or_create(
            email='survey@company.com',
            defaults={
                'name': 'IRIS Communications',
                'reply_to': 'support@company.com',
                'provider_type': Sender.ProviderType.SANDBOX,
                'is_active': True,
                'daily_limit': 80000,
                'hourly_limit': 3000,
                'per_minute_limit': 50
            }
        )
        self.stdout.write(self.style.SUCCESS(f"Configured default sender: {sender}"))

        # 3. ODK Sandbox Connection
        odk_conn, _ = ODKConnection.objects.get_or_create(
            name='Mock Sandbox ODK Central',
            defaults={
                'base_url': 'http://localhost:8000/sandbox/api/odk',
                'is_active': True,
                'is_mock_sandbox': True,
                'status': ODKConnection.Status.CONNECTED,
            }
        )
        proj, _ = ODKProject.objects.get_or_create(
            connection=odk_conn,
            odk_id=1,
            defaults={'name': 'Pulse Survey 2026', 'description': 'National Research Pulse Survey'}
        )
        form, _ = ODKForm.objects.get_or_create(
            project=proj,
            odk_xml_form_id='pulse_v1',
            defaults={'name': 'Pulse_V1', 'version': '1.0'}
        )
        ODKFieldMapping.objects.get_or_create(
            form=form,
            defaults={'job_id_field': 'job_id', 'email_field': 'email'}
        )
        self.stdout.write(self.style.SUCCESS("Configured default ODK Central sandbox connection and Pulse_V1 form."))

        # 4. Email Templates
        t1, _ = EmailTemplate.objects.get_or_create(
            name='Survey Invitation 2026',
            defaults={
                'subject': 'Pulse Survey 2026 – {{job_id}}',
                'preview_text': 'Your survey invitation and login details',
                'category': 'Survey Invitation',
                'html_content': (
                    '<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">'
                    '<h2 style="color: #1e293b;">Pulse Survey 2026</h2>'
                    '<p>Dear {{first_name}},</p>'
                    '<p>You have been selected to participate in our survey.</p>'
                    '<div style="background: #f8fafc; border-left: 4px solid #2563eb; padding: 12px 16px; margin: 20px 0;">'
                    '<p style="margin: 4px 0;"><strong>Email:</strong> {{email}}</p>'
                    '<p style="margin: 4px 0;"><strong>Password:</strong> {{login_password}}</p>'
                    '<p style="margin: 4px 0;"><strong>Job ID:</strong> {{job_id}}</p>'
                    '</div>'
                    '<p>Please complete your survey using the provided credentials:</p>'
                    '<p style="margin: 25px 0;"><a href="http://localhost:8000/sandbox/webmail/" style="background: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">Start Survey Now</a></p>'
                    '<p style="color: #64748b; font-size: 13px;">If you do not wish to receive further emails, you can <a href="{{unsubscribe_url}}" style="color: #64748b;">unsubscribe here</a>.</p>'
                    '<p>Regards,<br><strong>Research Team</strong></p>'
                    '</div>'
                ),
                'text_content': "Dear {{first_name}},\n\nYour survey details:\nEmail: {{email}}\nPassword: {{login_password}}\nJobID: {{job_id}}\n\nRegards,\nResearch Team"
            }
        )

        t2, _ = EmailTemplate.objects.get_or_create(
            name='Reminder: Please Complete Pulse Survey 2026',
            defaults={
                'subject': 'Reminder: Please Complete Pulse Survey 2026',
                'preview_text': 'Pending survey reminder',
                'category': 'Reminder',
                'html_content': (
                    '<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">'
                    '<h2 style="color: #d97706;">Reminder: Survey Pending</h2>'
                    '<p>Dear {{first_name}},</p>'
                    '<p>This is a reminder to complete your pending survey.</p>'
                    '<div style="background: #fef3c7; border-left: 4px solid #d97706; padding: 12px 16px; margin: 20px 0;">'
                    '<p style="margin: 4px 0;"><strong>Email:</strong> {{email}}</p>'
                    '<p style="margin: 4px 0;"><strong>Password:</strong> {{login_password}}</p>'
                    '<p style="margin: 4px 0;"><strong>Job ID:</strong> {{job_id}}</p>'
                    '</div>'
                    '<p>If you have already completed the survey, no further action is required.</p>'
                    '<p style="margin: 25px 0;"><a href="http://localhost:8000/sandbox/webmail/" style="background: #d97706; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">Complete Survey</a></p>'
                    '<p style="color: #64748b; font-size: 13px;">To stop receiving reminders, you can <a href="{{unsubscribe_url}}" style="color: #64748b;">unsubscribe</a>.</p>'
                    '<p>Regards,<br><strong>Research Team</strong></p>'
                    '</div>'
                ),
                'text_content': "Dear {{first_name}},\n\nReminder for survey:\nEmail: {{email}}\nPassword: {{login_password}}\nJobID: {{job_id}}\n\nRegards,\nResearch Team"
            }
        )
        self.stdout.write(self.style.SUCCESS("Created default survey invitation and reminder templates."))

        # 5. Contact Group
        group, _ = ContactGroup.objects.get_or_create(
            name='Pulse Survey 2026',
            defaults={'description': 'Primary respondent pool for Pulse Survey 2026'}
        )

        # 6. Sample Contacts from Section 92
        sample_data = [
            {
                'name': 'Ali Khan',
                'first_name': 'Ali',
                'last_name': 'Khan',
                'email': 'ali@example.com',
                'phone_number': '03001234567',
                'job_id': 'JOB-1025',
                'password': 'ABC123',
                'status': Contact.UsageStatus.UNUSED,
            },
            {
                'name': 'Sara Ahmed',
                'first_name': 'Sara',
                'last_name': 'Ahmed',
                'email': 'sara@example.com',
                'phone_number': '03009876543',
                'job_id': 'JOB-1026',
                'password': 'PWD456',
                'status': Contact.UsageStatus.UNUSED,
            },
            {
                'name': 'Usman Tariq',
                'first_name': 'Usman',
                'last_name': 'Tariq',
                'email': 'usman@example.com',
                'phone_number': '03123456789',
                'job_id': 'JOB-1027',
                'password': 'SEC789',
                'status': Contact.UsageStatus.UNUSED,
            },
            {
                'name': 'Fatima Noor',
                'first_name': 'Fatima',
                'last_name': 'Noor',
                'email': 'fatima@example.com',
                'phone_number': '03215554321',
                'job_id': 'JOB-1028',
                'password': 'PWD999',
                'status': Contact.UsageStatus.USED,
            },
        ]

        for s in sample_data:
            c, c_created = Contact.objects.get_or_create(
                email=s['email'],
                defaults={
                    'name': s['name'],
                    'first_name': s['first_name'],
                    'last_name': s['last_name'],
                    'phone_number': s['phone_number'],
                    'job_id': s['job_id'],
                    'status': s['status'],
                    'status_source': 'SEED'
                }
            )
            if c_created:
                c.login_password = s['password']
                c.save()
            group.contacts.add(c)

        self.stdout.write(self.style.SUCCESS("Seeded sample contacts (Ali Khan, Sara Ahmed, Usman Tariq, Fatima Noor)."))
