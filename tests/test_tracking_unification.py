import inspect
import json
from django.test import TestCase, Client, override_settings
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign
from apps.campaigns.services import (
    wrap_tracking, expand_tracking_placeholders,
    render_content_variables,
    create_shareable_tracking_link,
    get_or_create_recipient_tracking_link,
)
from apps.tracking.models import CampaignTrackingLink, CampaignLinkClickEvent
from apps.tracking.utils import (
    build_campaign_short_url, get_shortener_base_url,
)

# Synthetic ODK-shaped URLs (fake hosts + tokens only).
ODK_URL = 'https://odk.example.com/f/FAKE123?st=FakeToken&src=email'
ODK_OTHER = 'https://odk.example.com/f/OTHER9?st=OtherToken'
LOCAL_BASE = 'http://127.0.0.1:8000'
PROD_BASE = 'https://marketing.iriscommunications.cloud'

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

SHAREABLE = CampaignTrackingLink.LinkType.SHAREABLE
RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


def render_for_recipient(campaign, contact):
    """Mirrors production dispatch rendering for one recipient."""
    html = render_content_variables(
        expand_tracking_placeholders(
            campaign.html_content, campaign, contact),
        contact)
    return wrap_tracking(
        html, token_str='tok-' + str(contact.id),
        track_opens=True, track_clicks=campaign.track_clicks,
        campaign=campaign, contact=contact)


class UnifiedTrackingTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='uni_admin', email='uni@example.com',
            password='password123')
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX)
        self.group = ContactGroup.objects.create(name='Uni Group')
        self.contacts = []
        for first, email in (('Amina', 'uni_a@example.com'),
                             ('Bilal', 'uni_b@example.com')):
            c = Contact.objects.create(first_name=first, email=email)
            c.groups.add(self.group)
            self.contacts.append(c)
        self.campaign = Campaign.objects.create(
            name='Uni', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>Hi {{first_name}}</p><p>{{survey_tracking_url}}</p>',
            destination_url=ODK_URL, track_clicks=True)
        self.campaign.groups.add(self.group)

    def _post_link(self, payload, **extra):
        return self.client.post(
            '/api/campaigns/%d/tracking-links/' % self.campaign.id,
            data=json.dumps(payload), content_type='application/json',
            **extra)

    def _patch_link(self, link_id, payload):
        return self.client.patch(
            '/api/campaigns/%d/tracking-links/%d/'
            % (self.campaign.id, link_id),
            data=json.dumps(payload), content_type='application/json')


class EnvironmentTests(UnifiedTrackingTestCase):
    """Base URL comes from configuration in both paths, never elsewhere."""

    def test_localhost_base_flows_through_full_dispatch(self):
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            mails = [render_for_recipient(self.campaign, c)
                     for c in self.contacts]
        for html in mails:
            self.assertIn(LOCAL_BASE + '/c/', html)
            self.assertNotIn(PROD_BASE, html)
            self.assertNotIn('odk.example.com', html)
        links = CampaignTrackingLink.objects.filter(
            campaign=self.campaign, link_type=RECIPIENT)
        self.assertEqual(links.count(), 2)
        for lk in links:
            self.assertTrue(lk.short_url.startswith(LOCAL_BASE + '/c/'))

    def test_production_base_flows_through_step1_and_step5(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
            self.assertEqual(r.status_code, 201)
            self.assertTrue(r.json()['short_url'].startswith(PROD_BASE + '/c/'))
            html = render_for_recipient(self.campaign, self.contacts[0])
            self.assertIn(PROD_BASE + '/c/', html)

    def test_no_hardcoded_production_domain_in_tracking_code(self):
        from apps.campaigns import services as campaign_services
        from apps.tracking import utils as tracking_utils
        from apps.tracking import views as tracking_views
        fns = [
            tracking_utils.get_shortener_base_url,
            tracking_utils.build_campaign_short_url,
            campaign_services.create_shareable_tracking_link,
            campaign_services.get_or_create_recipient_tracking_link,
            campaign_services.expand_tracking_placeholders,
            campaign_services.wrap_tracking,
            tracking_views.CampaignLinkRedirectView.get,
        ]
        for fn in fns:
            with self.subTest(fn=fn.__qualname__):
                self.assertNotIn('marketing.iriscommunications.cloud',
                                 inspect.getsource(fn))

    def test_no_request_host_contamination(self):
        # Even when the inbound request carries a foreign Host, minted
        # URLs use the configured base.
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE,
                               ALLOWED_HOSTS=['testserver',
                                              'evil.example.com']):
            r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL},
                                HTTP_HOST='evil.example.com')
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()['short_url'].startswith(LOCAL_BASE + '/c/'))
        self.assertNotIn('evil.example.com', r.json()['short_url'])
        # The builder API itself takes no request: contamination is
        # impossible by construction.
        for fn in (get_shortener_base_url, build_campaign_short_url):
            self.assertNotIn('request', inspect.signature(fn).parameters)


class Step1LifecycleTests(UnifiedTrackingTestCase):
    """Step 1 create + edit through the canonical service and API."""

    def test_create_shareable_link_canonical(self):
        link = create_shareable_tracking_link(
            self.campaign, name='FB Ad', destination_url=ODK_OTHER)
        self.assertEqual(link.link_type, SHAREABLE)
        self.assertIsNone(link.contact)
        self.assertEqual(link.campaign, self.campaign)
        self.assertEqual(link.name, 'FB Ad')
        self.assertEqual(link.destination_url, ODK_OTHER)
        self.assertTrue(link.short_url.startswith('https://'))
        self.assertIn('/c/' + link.tracking_token, link.short_url)
        self.assertNotIn('?', link.short_url)
        click = self.anon.get('/c/' + link.tracking_token + '/',
                              HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_OTHER)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertIsNone(evt.contact)

    def test_create_rejects_dead_link(self):
        blank = Campaign.objects.create(
            name='Blank', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content='<p>x</p>',
            destination_url='')
        with self.assertRaises(ValueError):
            create_shareable_tracking_link(blank, name='X')
        with self.assertRaises(ValueError):
            create_shareable_tracking_link(
                self.campaign, name='X', destination_url='notaurl')
        self.assertEqual(
            CampaignTrackingLink.objects.filter(campaign=blank).count(), 0)

    def test_edit_destination_keeps_token_and_redirects(self):
        r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
        link_id = r.json()['id']
        token = r.json()['tracking_token']
        url = r.json()['short_url']
        r = self._patch_link(link_id, {'destination_url': ODK_OTHER})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['tracking_token'], token)
        self.assertEqual(r.json()['short_url'], url)
        click = self.anon.get('/c/' + token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_OTHER)

    def test_edit_name_visible_in_shareable_report(self):
        r = self._post_link({'name': 'Old', 'destination_url': ODK_URL})
        link_id = r.json()['id']
        r = self._patch_link(link_id, {'name': 'New Label'})
        self.assertEqual(r.status_code, 200)
        api = self.client.get(
            '/api/campaigns/%d/report/shareable-links/' % self.campaign.id)
        self.assertEqual(api.status_code, 200)
        names = [e['name'] for e in api.json()['links']]
        self.assertIn('New Label', names)
        self.assertNotIn('Old', names)

    def test_disable_enable_link(self):
        r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
        link_id = r.json()['id']
        token = r.json()['tracking_token']
        r = self._patch_link(link_id, {'is_active': False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['is_active'])
        click = self.anon.get('/c/' + token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 410)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)
        r = self._patch_link(link_id, {'is_active': True})
        self.assertEqual(r.status_code, 200)
        click = self.anon.get('/c/' + token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 1)

    def test_blank_name_stored_empty_report_fallback(self):
        r = self._post_link({'name': '', 'destination_url': ODK_URL})
        self.assertEqual(r.status_code, 201)
        link = CampaignTrackingLink.objects.get(id=r.json()['id'])
        self.assertEqual(link.name, '')
        api = self.client.get(
            '/api/campaigns/%d/report/shareable-links/' % self.campaign.id)
        names = [e['name'] for e in api.json()['links']]
        self.assertIn('Untitled link', names)

    def test_regenerate_rotates_token_only(self):
        r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
        link_id = r.json()['id']
        old_token = r.json()['tracking_token']
        r = self.client.post(
            '/api/campaigns/%d/tracking-links/%d/regenerate/'
            % (self.campaign.id, link_id))
        self.assertEqual(r.status_code, 200)
        new_token = r.json()['tracking_token']
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(r.json()['name'], 'Ad')
        self.assertEqual(r.json()['destination_url'], ODK_URL)
        gone = self.anon.get('/c/' + old_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(gone.status_code, 404)
        click = self.anon.get('/c/' + new_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)


class CrossSystemTests(UnifiedTrackingTestCase):
    """Step 1 and Step 5 share one system end to end."""

    def test_step5_uses_campaign_default_destination(self):
        html = render_for_recipient(self.campaign, self.contacts[0])
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contacts[0],
            link_type=RECIPIENT)
        self.assertEqual(link.destination_url, ODK_URL)
        self.assertIn(link.short_url, html)
        self.assertNotIn('<a ', html)
        click = self.anon.get('/c/' + link.tracking_token + '/',
                              HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)

    def test_same_builder_step1_step5(self):
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
            share_url = r.json()['short_url']
            render_for_recipient(self.campaign, self.contacts[0])
            recip = CampaignTrackingLink.objects.get(
                campaign=self.campaign, contact=self.contacts[0],
                link_type=RECIPIENT)
            base = get_shortener_base_url()
        self.assertTrue(share_url.startswith(base + '/c/'))
        self.assertTrue(recip.short_url.startswith(base + '/c/'))
        self.assertEqual(base, LOCAL_BASE)

    def test_same_redirect_and_event_infrastructure(self):
        r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
        share_token = r.json()['tracking_token']
        render_for_recipient(self.campaign, self.contacts[0])
        recip = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contacts[0],
            link_type=RECIPIENT)
        for token in (share_token, recip.tracking_token):
            click = self.anon.get('/c/' + token + '/',
                                  HTTP_USER_AGENT=HUMAN_UA)
            self.assertEqual(click.status_code, 302)
            self.assertEqual(click.url, ODK_URL)
        events = CampaignLinkClickEvent.objects.filter(
            campaign=self.campaign).order_by('id')
        self.assertEqual(events.count(), 2)
        by_type = {e.link.link_type: e for e in events}
        self.assertIsNone(by_type[SHAREABLE].contact)
        self.assertEqual(by_type[RECIPIENT].contact, self.contacts[0])

    def test_same_destination_resolution_both_link_types(self):
        blank_share = create_shareable_tracking_link(self.campaign, name='S')
        blank_recip = CampaignTrackingLink.objects.create(
            campaign=self.campaign, contact=self.contacts[0],
            link_type=RECIPIENT, name='R',
            tracking_token='ZZRESOLV1',
            short_url=build_campaign_short_url('ZZRESOLV1'),
            destination_url='')
        self.assertEqual(blank_share.resolve_destination(), ODK_URL)
        self.assertEqual(blank_recip.resolve_destination(), ODK_URL)
        for token in (blank_share.tracking_token, 'ZZRESOLV1'):
            click = self.anon.get('/c/' + token + '/',
                                  HTTP_USER_AGENT=HUMAN_UA)
            self.assertEqual(click.status_code, 302)
            self.assertEqual(click.url, ODK_URL)

    def test_same_reporting_infrastructure(self):
        r = self._post_link({'name': 'Ad', 'destination_url': ODK_URL})
        self.anon.get('/c/' + r.json()['tracking_token'] + '/',
                      HTTP_USER_AGENT=HUMAN_UA)
        render_for_recipient(self.campaign, self.contacts[0])
        recip = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contacts[0],
            link_type=RECIPIENT)
        self.anon.get('/c/' + recip.tracking_token + '/',
                      HTTP_USER_AGENT=HUMAN_UA)

        clicks = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id).json()
        self.assertEqual(clicks['total_events'], 1)
        self.assertEqual(
            clicks['events'][0]['contact_email'], 'uni_a@example.com')
        self.assertEqual(
            clicks['events'][0]['short_url'], recip.short_url)
        self.assertEqual(
            clicks['events'][0]['destination_url'], ODK_URL)

        share = self.client.get(
            '/api/campaigns/%d/report/shareable-links/'
            % self.campaign.id).json()
        self.assertEqual(len(share['recent_events']), 1)
        self.assertEqual(
            share['recent_events'][0]['link_name'], 'Ad')

        self.assertEqual(
            CampaignLinkClickEvent.objects.filter(
                campaign=self.campaign).count(), 2)
