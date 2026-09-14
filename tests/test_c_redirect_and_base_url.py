import json
from django.test import TestCase, Client, override_settings
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign
from apps.campaigns.services import wrap_tracking
from apps.tracking.models import CampaignTrackingLink, CampaignLinkClickEvent
from apps.tracking.utils import build_campaign_short_url

# Synthetic ODK-shaped URLs (same structure as production URLs, but fake
# hosts + tokens). Never use a real survey token in tests.
ODK_URL = 'https://odk.example.com/f/FAKE123?st=FakeToken&src=email'
ODK_OTHER = 'https://odk.example.com/f/OTHER9?st=OtherToken'

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

SHAREABLE = CampaignTrackingLink.LinkType.SHAREABLE
RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


class Step1TrackingLinkTests(TestCase):
    """Step 1 link generation: absolute URLs, no dead links."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='rd_admin', email='rd@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='RD Group')
        self.contact = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9301',
        )
        self.contact.groups.add(self.group)

    def _campaign(self, destination_url=ODK_URL, status=None):
        camp = Campaign.objects.create(
            name='RD Campaign',
            subject='Hello',
            sender=self.sender,
            status=status or Campaign.Status.ACTIVE,
            html_content='<p>Hi</p>',
            destination_url=destination_url,
            track_clicks=True,
        )
        camp.groups.add(self.group)
        return camp

    def test_create_blank_destination_blank_default_rejected(self):
        camp = self._campaign(destination_url='')
        resp = self.client.post(
            '/api/campaigns/%d/tracking-links/' % camp.id,
            data=json.dumps({'name': 'Ad', 'destination_url': ''}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('destination', json.dumps(resp.json()).lower())
        self.assertEqual(
            CampaignTrackingLink.objects.filter(campaign=camp).count(), 0)

    def test_create_blank_override_falls_back_to_campaign_default(self):
        camp = self._campaign(destination_url=ODK_URL)
        resp = self.client.post(
            '/api/campaigns/%d/tracking-links/' % camp.id,
            data=json.dumps({'name': 'Ad', 'destination_url': ''}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data['short_url'].startswith('https://'))
        self.assertIn('/c/' + data['tracking_token'], data['short_url'])
        self.assertNotIn('?url=', data['short_url'])

        click = self.anon.get('/c/' + data['tracking_token'] + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(
            link__tracking_token=data['tracking_token'])
        self.assertIsNone(evt.contact)
        self.assertEqual(evt.campaign, camp)

    def test_create_with_override_redirects_to_override(self):
        camp = self._campaign(destination_url=ODK_URL)
        resp = self.client.post(
            '/api/campaigns/%d/tracking-links/' % camp.id,
            data=json.dumps({'name': 'Ad', 'destination_url': ODK_OTHER}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data['short_url'].startswith('https://'))
        self.assertNotIn('?url=', data['short_url'])
        click = self.anon.get('/c/' + data['tracking_token'] + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_OTHER)

    def test_update_blanking_destination_rejected(self):
        camp = self._campaign(destination_url='')
        link = CampaignTrackingLink.objects.create(
            campaign=camp, name='Ad', tracking_token='UPD8X000',
            short_url='https://marketing.iriscommunications.cloud/c/UPD8X000',
            destination_url=ODK_URL)
        resp = self.client.patch(
            '/api/campaigns/%d/tracking-links/%d/' % (camp.id, link.id),
            data=json.dumps({'destination_url': ''}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        link.refresh_from_db()
        self.assertEqual(link.destination_url, ODK_URL)


class CRedirectFailureModeTests(TestCase):
    """Exact /c/ symptoms: only a live row with a valid destination 302s."""

    def setUp(self):
        self.anon = Client()
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.camp = Campaign.objects.create(
            name='C', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content='<p>x</p>',
            destination_url='',
        )

    def _link(self, token, dest='', active=True, campaign=None):
        return CampaignTrackingLink.objects.create(
            campaign=campaign or self.camp, name='L', tracking_token=token,
            short_url='https://marketing.iriscommunications.cloud/c/' + token,
            destination_url=dest, is_active=active)

    def test_unknown_token_404_without_event(self):
        r = self.anon.get('/c/NOPE1234/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_inactive_link_410_without_event(self):
        self._link('OFF00001', dest=ODK_URL, active=False)
        r = self.anon.get('/c/OFF00001/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r.status_code, 410)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_paused_campaign_410_without_event(self):
        paused = Campaign.objects.create(
            name='P', subject='S', sender=self.sender,
            status=Campaign.Status.PAUSED, html_content='<p>x</p>',
            destination_url=ODK_URL)
        self._link('PAUSED01', dest=ODK_URL, campaign=paused)
        r = self.anon.get('/c/PAUSED01/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r.status_code, 410)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_blank_destination_400_without_event(self):
        self._link('BLANK001', dest='')
        r = self.anon.get('/c/BLANK001/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_shareable_click_records_anonymous_event_and_redirects(self):
        link = self._link('GOOD0001', dest=ODK_URL)
        r = self.anon.get('/c/GOOD0001/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertIsNone(evt.contact)
        self.assertEqual(evt.click_type, 'HUMAN')
        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)
        self.assertEqual(link.human_click_count, 1)


class TrackingBaseUrlTests(TestCase):
    """Generated tracking URLs are absolute and configuration-driven."""

    def setUp(self):
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='BU Group')
        self.contact = Contact.objects.create(
            first_name='Ali', email='ali@example.com',
        )
        self.contact.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='BU', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content='<p>x</p>',
            destination_url=ODK_URL, track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    def test_short_url_uses_configured_base(self):
        with override_settings(SHORTENER_BASE_URL='https://short.example.test'):
            self.assertEqual(
                build_campaign_short_url('AbC123Xy'),
                'https://short.example.test/c/AbC123Xy')
        with override_settings(
                SHORTENER_BASE_URL='https://marketing.iriscommunications.cloud'):
            self.assertEqual(
                build_campaign_short_url('p7iz4VA0'),
                'https://marketing.iriscommunications.cloud/c/p7iz4VA0')

    def test_explicit_localhost_base_is_honoured(self):
        # A locally-configured base must survive: mapping it to production
        # mints URLs whose rows live in a different database (dead links).
        with override_settings(SHORTENER_BASE_URL='http://localhost:8000'):
            self.assertEqual(
                build_campaign_short_url('AbC123Xy'),
                'http://localhost:8000/c/AbC123Xy')
        with override_settings(SHORTENER_BASE_URL='http://127.0.0.1:8000'):
            self.assertEqual(
                build_campaign_short_url('AbC123Xy'),
                'http://127.0.0.1:8000/c/AbC123Xy')

    def test_empty_shortener_falls_back_to_base_tracking_url(self):
        with override_settings(SHORTENER_BASE_URL='',
                               BASE_TRACKING_URL='https://trk.example.test'):
            self.assertEqual(
                build_campaign_short_url('AbC123Xy'),
                'https://trk.example.test/c/AbC123Xy')

    def test_generated_recipient_url_is_absolute_without_query(self):
        stored = ODK_URL.replace('&', '&amp;')
        html = (
            '<p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
            'data-original-url="' + stored + '" data-link-name="Survey" '
            'data-track="true">Take Survey</a></p>'
        )
        out = wrap_tracking(
            html, token_str='t1', campaign=self.campaign, contact=self.contact)
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact, link_type=RECIPIENT)
        self.assertTrue(link.short_url.startswith('https://'))
        self.assertNotIn('?', link.short_url)
        self.assertNotIn('url=', link.short_url)
        self.assertNotIn(ODK_URL, link.short_url)
        self.assertIn(link.short_url, out)
