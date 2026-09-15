from django.test import TestCase
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.reminders.services import process_single_campaign_message
from apps.sandbox.models import SandboxEmail


class CampaignPauseSemanticsTests(TestCase):
    """Pause stops sending/reminders (unchanged behavior). It must NOT
    disable previously issued tracking links (see redirect tests)."""

    def setUp(self):
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX, is_active=True)
        self.group = ContactGroup.objects.create(name='Pause Group')
        self.contact = Contact.objects.create(
            first_name='Amina', email='pause_a@example.com',
            status=Contact.UsageStatus.UNUSED,
            email_status=Contact.EmailStatus.ACTIVE)
        self.group.contacts.add(self.contact)
        self.campaign = Campaign.objects.create(
            name='Pause', subject='S', sender=self.sender,
            status=Campaign.Status.PAUSED, html_content='<p>x</p>',
            destination_url='https://odk.example.com/f/1')
        self.campaign.groups.add(self.group)

    def _queued_message(self):
        return CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact,
            message_type=CampaignMessage.MessageType.INITIAL,
            reminder_sequence=0, to_email=self.contact.email,
            subject='S', status=CampaignMessage.Status.QUEUED)

    def test_paused_campaign_skips_send(self):
        msg = self._queued_message()
        self.assertFalse(process_single_campaign_message(msg.id))
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.SKIPPED)
        self.assertEqual(
            msg.skip_reason, CampaignMessage.SkipReason.CAMPAIGN_PAUSED)
        self.assertEqual(SandboxEmail.objects.count(), 0)

    def test_resumed_campaign_sends(self):
        self.campaign.status = Campaign.Status.ACTIVE
        self.campaign.save(update_fields=['status'])
        msg = self._queued_message()
        self.assertTrue(process_single_campaign_message(msg.id))
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.DELIVERED)
        self.assertEqual(SandboxEmail.objects.count(), 1)
