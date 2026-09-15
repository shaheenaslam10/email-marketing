from django.test import TestCase, Client
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.tracking.models import (
    CampaignTrackingLink, CampaignLinkClickEvent, EmailEvent,
)
from apps.tracking.utils import build_campaign_short_url

# Synthetic ODK-shaped URLs (fake hosts + tokens only).
ODK_URL = 'https://odk.example.com/f/FAKE123?st=FakeToken&src=email'

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

SHAREABLE = CampaignTrackingLink.LinkType.SHAREABLE
RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


class RedirectTypeParityTests(TestCase):
    """The /c/ redirect is type-symmetric: RECIPIENT links never fail
    merely for being RECIPIENT. Guards are data-driven (existence,
    active flag, campaign status, destination validity) and identical
    for both link types; only attribution differs, by design."""

    def setUp(self):
        self.anon = Client()
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX)
        self.group = ContactGroup.objects.create(name='Par Group')
        self.contact = Contact.objects.create(
            first_name='Amina', email='par_a@example.com')
        self.contact.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='Par', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content='<p>x</p>',
            destination_url=ODK_URL)

    def _link(self, token, dest=ODK_URL, contact=None, active=True):
        return CampaignTrackingLink.objects.create(
            campaign=self.campaign, contact=contact,
            link_type=RECIPIENT if contact is not None else SHAREABLE,
            name='L', tracking_token=token,
            short_url=build_campaign_short_url(token),
            destination_url=dest, is_active=active)

    def _click(self, token):
        return self.anon.get('/c/' + token + '/', HTTP_USER_AGENT=HUMAN_UA)

    def test_recipient_with_message_redirects_and_attributes(self):
        link = self._link('MSG00001', contact=self.contact)
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact,
            message_type=CampaignMessage.MessageType.INITIAL,
            reminder_sequence=0, to_email=self.contact.email,
            subject='S', status=CampaignMessage.Status.DELIVERED)
        r = self._click('MSG00001')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(evt.contact, self.contact)
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)
        self.assertIsNotNone(msg.clicked_at)
        self.assertEqual(
            EmailEvent.objects.filter(
                message=msg,
                event_type=EmailEvent.EventType.CLICK).count(), 1)

    def test_recipient_without_message_redirects(self):
        link = self._link('NOMSG001', contact=self.contact)
        r = self._click('NOMSG001')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(evt.contact, self.contact)
        self.assertEqual(
            EmailEvent.objects.filter(
                event_type=EmailEvent.EventType.CLICK).count(), 0)

    def test_invalid_own_destination_wins_over_valid_default(self):
        # Resolution precedence is own-destination-first: a stored but
        # invalid override fails closed (400) instead of silently
        # falling back. Creation/update validation prevents new rows
        # from entering this state; fossil rows are fixed as data.
        link = self._link('JUNK0001', dest='notaurl', contact=self.contact)
        r = self._click('JUNK0001')
        self.assertEqual(r.status_code, 400)
        self.assertIn('No valid destination URL', r.content.decode())
        self.assertEqual(
            CampaignLinkClickEvent.objects.filter(link=link).count(), 0)
        link.refresh_from_db()
        self.assertEqual(link.click_count, 0)

    def test_inactive_recipient_matches_inactive_shareable(self):
        r_link = self._link('INACTR01', contact=self.contact, active=False)
        s_link = self._link('INACTS01', active=False)
        for token in ('INACTR01', 'INACTS01'):
            r = self._click(token)
            self.assertEqual(r.status_code, 410)
            self.assertIn('disabled by the campaign owner',
                          r.content.decode())
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)
        r_link.refresh_from_db()
        s_link.refresh_from_db()
        self.assertEqual(r_link.click_count, 0)
        self.assertEqual(s_link.click_count, 0)

    def test_paused_campaign_recipient_redirects_and_attributes(self):
        link = self._link('PAUSR001', contact=self.contact)
        self.campaign.status = Campaign.Status.PAUSED
        self.campaign.save(update_fields=['status'])
        r = self._click('PAUSR001')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(evt.contact, self.contact)
