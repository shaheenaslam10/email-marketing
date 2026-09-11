from django.test import TestCase
from django.utils import timezone
from apps.contacts.models import Contact, ContactStatusHistory
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.reminders.models import ReminderConfiguration, ReminderCycle
from apps.reminders.services import execute_reminder_cycle, process_single_campaign_message, launch_initial_campaign
from apps.sandbox.models import MockODKSubmission, SandboxEmail
from apps.odk.models import ODKConnection, ODKProject, ODKForm, ODKFieldMapping
from apps.odk.services import run_odk_sync
from apps.campaigns.services import render_content_variables


class SurveyReminderCriticalTests(TestCase):
    def setUp(self):
        # Clear sandbox emails
        SandboxEmail.objects.all().delete()
        MockODKSubmission.objects.all().delete()

        # Create sender
        self.sender = Sender.objects.create(
            name="Test Sender",
            email="survey@company.com",
            provider_type=Sender.ProviderType.SANDBOX,
            is_active=True
        )

        # Create group
        self.group = ContactGroup.objects.create(name="Survey Test Group")

        # Create ODK Sandbox setup
        self.odk_conn = ODKConnection.objects.create(
            name="Test ODK Central",
            base_url="http://localhost:8000/sandbox/api/odk",
            is_mock_sandbox=True,
            status=ODKConnection.Status.CONNECTED
        )
        self.odk_project = ODKProject.objects.create(
            connection=self.odk_conn,
            odk_id=1,
            name="Pulse 2026"
        )
        self.odk_form = ODKForm.objects.create(
            project=self.odk_project,
            odk_xml_form_id="pulse_v1",
            name="Pulse_V1"
        )
        self.mapping = ODKFieldMapping.objects.create(
            form=self.odk_form,
            job_id_field="job_id"
        )

        # Create Campaign with Automated Reminders
        self.campaign = Campaign.objects.create(
            name="Pulse Survey Test Campaign",
            campaign_type=Campaign.Type.SURVEY_REMINDER,
            status=Campaign.Status.ACTIVE,
            sender=self.sender,
            odk_form=self.odk_form,
            subject="Pulse Survey 2026 - {{job_id}}",
            html_content="<p>Dear {{first_name}}, Password: {{login_password}}, JobID: {{job_id}}</p>"
        )
        self.campaign.groups.add(self.group)

        self.reminder_cfg = ReminderConfiguration.objects.create(
            campaign=self.campaign,
            enabled=True,
            interval_value=2,
            interval_unit=ReminderConfiguration.Unit.DAYS,
            max_reminders=5,
            sync_odk_before_send=True,
            stop_when_used=True
        )

    def test_1_contact_unused_no_odk_submission_reminder_sent(self):
        """Test 1: Contact = Unused, No ODK submission -> Reminder sent."""
        contact = Contact.objects.create(
            name="Ali Khan",
            first_name="Ali",
            email="ali@example.com",
            job_id="JOB-1025",
            status=Contact.UsageStatus.UNUSED
        )
        contact.login_password = "SecretPassword123"
        contact.save()
        self.group.contacts.add(contact)

        # Run reminder cycle
        cycle = execute_reminder_cycle(self.campaign, manual_trigger=True)

        self.assertEqual(cycle.sent_count, 1)
        self.assertEqual(cycle.eligible_count, 1)

        # Verify message created and delivered
        msg = CampaignMessage.objects.filter(
            campaign=self.campaign,
            contact=contact,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1
        ).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.status, CampaignMessage.Status.DELIVERED)

        # Verify password decrypted in rendered sandbox email
        sent_email = SandboxEmail.objects.filter(to_email=contact.email).first()
        self.assertIsNotNone(sent_email)
        self.assertIn("SecretPassword123", sent_email.html_content)
        self.assertIn("JOB-1025", sent_email.html_content)

    def test_2_contact_used_reminder_not_sent(self):
        """Test 2: Contact = Used -> Reminder not sent."""
        contact = Contact.objects.create(
            name="Sara Ahmed",
            email="sara@example.com",
            job_id="JOB-1026",
            status=Contact.UsageStatus.USED
        )
        self.group.contacts.add(contact)

        cycle = execute_reminder_cycle(self.campaign, manual_trigger=True)

        self.assertEqual(cycle.sent_count, 0)
        self.assertEqual(cycle.eligible_count, 0)
        # Verify no reminder message was queued or sent
        self.assertFalse(
            CampaignMessage.objects.filter(
                campaign=self.campaign,
                contact=contact,
                message_type=CampaignMessage.MessageType.REMINDER
            ).exists()
        )

    def test_3_contact_initially_unused_odk_submission_appears_before_reminder(self):
        """Test 3: Contact initially Unused, ODK submission appears before reminder -> Status becomes Used, reminder not sent."""
        contact = Contact.objects.create(
            name="Usman Tariq",
            email="usman@example.com",
            job_id="JOB-1027",
            status=Contact.UsageStatus.UNUSED
        )
        self.group.contacts.add(contact)

        # Simulate respondent submitting survey in ODK Central
        MockODKSubmission.objects.create(
            submission_id="uuid:sub-1027",
            project_id="1",
            form_id="pulse_v1",
            job_id="JOB-1027",
            respondent_email=contact.email
        )

        # Execute reminder cycle (which runs pre-reminder ODK sync first)
        cycle = execute_reminder_cycle(self.campaign, manual_trigger=True)

        # Contact must now be USED
        contact.refresh_from_db()
        self.assertEqual(contact.status, Contact.UsageStatus.USED)
        self.assertEqual(contact.odk_submission_id, "uuid:sub-1027")

        # Reminder must not be sent
        self.assertEqual(cycle.sent_count, 0)
        self.assertFalse(
            CampaignMessage.objects.filter(
                campaign=self.campaign,
                contact=contact,
                message_type=CampaignMessage.MessageType.REMINDER
            ).exists()
        )

    def test_4_contact_queued_as_unused_becomes_used_before_worker_sends(self):
        """
        Test 4: Contact was queued as Unused, status becomes Used before worker sends
        -> Worker performs final status check (contact.refresh_from_db()), email skipped.
        """
        contact = Contact.objects.create(
            name="Zain Malik",
            email="zain@example.com",
            job_id="JOB-1029",
            status=Contact.UsageStatus.UNUSED
        )
        self.group.contacts.add(contact)

        # Message is queued when status was UNUSED
        msg = CampaignMessage.objects.create(
            campaign=self.campaign,
            contact=contact,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1,
            to_email=contact.email,
            subject=self.campaign.subject,
            status=CampaignMessage.Status.QUEUED
        )

        # Status changes to USED right before worker dispatches message
        contact.status = Contact.UsageStatus.USED
        contact.save(update_fields=['status'])

        # Worker processes message
        sent = process_single_campaign_message(msg.id)

        self.assertFalse(sent)
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.SKIPPED)
        self.assertEqual(msg.skip_reason, CampaignMessage.SkipReason.CONTACT_USED)

    def test_5_contact_unsubscribed_reminder_not_sent(self):
        """Test 5: Contact unsubscribed -> Reminder not sent."""
        contact = Contact.objects.create(
            name="Hina Bilal",
            email="hina@example.com",
            job_id="JOB-1030",
            status=Contact.UsageStatus.UNUSED,
            unsubscribed=True
        )
        self.group.contacts.add(contact)

        cycle = execute_reminder_cycle(self.campaign, manual_trigger=True)

        self.assertEqual(cycle.sent_count, 0)
        self.assertFalse(
            CampaignMessage.objects.filter(
                campaign=self.campaign,
                contact=contact,
                message_type=CampaignMessage.MessageType.REMINDER
            ).exists()
        )

    def test_6_contact_hard_bounced_reminder_not_sent(self):
        """Test 6: Contact hard bounced -> Reminder not sent."""
        contact = Contact.objects.create(
            name="Bounced User",
            email="bounced@example.com",
            job_id="JOB-1031",
            status=Contact.UsageStatus.UNUSED,
            email_status=Contact.EmailStatus.HARD_BOUNCE
        )
        self.group.contacts.add(contact)

        cycle = execute_reminder_cycle(self.campaign, manual_trigger=True)

        self.assertEqual(cycle.sent_count, 0)
        self.assertFalse(
            CampaignMessage.objects.filter(
                campaign=self.campaign,
                contact=contact,
                message_type=CampaignMessage.MessageType.REMINDER
            ).exists()
        )

    def test_7_reminder_worker_retries_task_duplicate_prevented(self):
        """Test 7: Reminder worker retries task -> Same reminder is not sent twice."""
        contact = Contact.objects.create(
            name="Bilal Shah",
            email="bilal@example.com",
            job_id="JOB-1032",
            status=Contact.UsageStatus.UNUSED
        )
        self.group.contacts.add(contact)

        # Cycle 1
        cycle1 = execute_reminder_cycle(self.campaign, manual_trigger=True)
        self.assertEqual(cycle1.sent_count, 1)

        # Attempt to queue the exact same reminder sequence for this contact
        msg_count_before = CampaignMessage.objects.filter(
            campaign=self.campaign,
            contact=contact,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1
        ).count()
        self.assertEqual(msg_count_before, 1)

        # Even if re-invoked with same cycle number, duplicate message must not be created
        with self.assertRaises(Exception):
            CampaignMessage.objects.create(
                campaign=self.campaign,
                contact=contact,
                message_type=CampaignMessage.MessageType.REMINDER,
                reminder_sequence=1,
                to_email=contact.email,
                subject="Duplicate Test"
            )

    def test_8_import_contacts_with_identical_job_id_or_link(self):
        """Test 8: Contacts sharing identical survey Link/JobID do NOT overwrite each other."""
        from apps.imports.services import execute_import

        shared_link = "https://odk.iriscommunications.cloud/f/survey123?st=TOKEN123"
        rows = [
            {'Email': 'person1@example.com', 'FirstName': 'Person One', 'Link': shared_link, 'login': '111', 'password': 'p1'},
            {'Email': 'person2@example.com', 'FirstName': 'Person Two', 'Link': shared_link, 'login': '222', 'password': 'p2'},
            {'Email': 'person3@example.com', 'FirstName': 'Person Three', 'Link': shared_link, 'login': '333', 'password': 'p3'},
        ]
        mapping = {
            'email': 'Email',
            'name': 'FirstName',
            'job_id': 'Link',
            'phone_number': 'login',
            'login_password': 'password'
        }

        result = execute_import(rows, mapping, duplicate_strategy='UPDATE', target_group=self.group)

        self.assertEqual(result['created'], 3)
        self.assertEqual(Contact.objects.filter(email='person1@example.com').count(), 1)
        self.assertEqual(Contact.objects.filter(email='person2@example.com').count(), 1)
        self.assertEqual(Contact.objects.filter(email='person3@example.com').count(), 1)
        self.assertEqual(self.group.contacts.filter(job_id=shared_link).count(), 3)

