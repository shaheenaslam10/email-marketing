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
    get_or_create_recipient_tracking_link,
)
from apps.tracking.models import CampaignTrackingLink, CampaignLinkClickEvent

# Synthetic ODK-shaped URLs (fake hosts + tokens only).
ODK_URL = 'https://odk.example.com/f/FAKE123?st=FakeToken&src=email'
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


class DerivedShortUrlTests(TestCase):
    """Stored absolute URLs are never emitted; the public URL is always
    derived from the token with the CURRENT configured base."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='der_admin', email='der@example.com',
            password='password123')
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()
        self.sender = Sender.objects.create(
            name='S', email='s@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX)
        self.group = ContactGroup.objects.create(name='Der Group')
        self.contact = Contact.objects.create(
            first_name='Amina', email='der_a@example.com')
        self.contact.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='Der', subject='S', sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>{{survey_tracking_url}}</p>',
            destination_url=ODK_URL, track_clicks=True)
        self.campaign.groups.add(self.group)
        self.links_url = (
            '/api/campaigns/%d/tracking-links/' % self.campaign.id)

    def _seed_recipient_row_under_prod(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            link = get_or_create_recipient_tracking_link(
                self.campaign, self.contact, ODK_URL, 'Survey')
        self.assertTrue(link.short_url.startswith(PROD_BASE + '/c/'))
        return link

    def test_step5_reused_row_emits_current_base_same_token(self):
        seeded = self._seed_recipient_row_under_prod()
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            html = render_for_recipient(self.campaign, self.contact)
        self.assertIn(LOCAL_BASE + '/c/' + seeded.tracking_token, html)
        self.assertNotIn(PROD_BASE, html)
        # Deduplication preserved: same row, same token, no regeneration.
        self.assertEqual(
            CampaignTrackingLink.objects.filter(
                campaign=self.campaign, contact=self.contact,
                link_type=RECIPIENT).count(), 1)
        self.assertEqual(
            CampaignTrackingLink.objects.get(
                campaign=self.campaign, contact=self.contact,
                link_type=RECIPIENT).id, seeded.id)

    def test_step5_click_after_base_change_redirects(self):
        seeded = self._seed_recipient_row_under_prod()
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            render_for_recipient(self.campaign, self.contact)
        click = self.anon.get('/c/' + seeded.tracking_token + '/',
                              HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=seeded)
        self.assertEqual(evt.contact, self.contact)

    def test_step1_list_derives_current_base(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            r = self.client.post(
                self.links_url,
                data=json.dumps(
                    {'name': 'Ad', 'destination_url': ODK_URL}),
                content_type='application/json')
        self.assertEqual(r.status_code, 201)
        token = r.json()['tracking_token']
        self.assertTrue(r.json()['short_url'].startswith(PROD_BASE + '/c/'))
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            r = self.client.get(self.links_url)
        self.assertEqual(r.status_code, 200)
        by_id = {e['tracking_token']: e for e in r.json()['links']}
        self.assertEqual(
            by_id[token]['short_url'], LOCAL_BASE + '/c/' + token)

    def test_step1_detail_patch_response_derives(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            r = self.client.post(
                self.links_url,
                data=json.dumps(
                    {'name': 'Ad', 'destination_url': ODK_URL}),
                content_type='application/json')
        link_id = r.json()['id']
        token = r.json()['tracking_token']
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            r = self.client.patch(
                self.links_url + '%d/' % link_id,
                data=json.dumps({'name': 'Renamed'}),
                content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['short_url'], LOCAL_BASE + '/c/' + token)
        self.assertEqual(r.json()['tracking_token'], token)

    def test_recipient_click_activity_derives(self):
        seeded = self._seed_recipient_row_under_prod()
        CampaignLinkClickEvent.objects.create(
            link=seeded, campaign=self.campaign, contact=self.contact,
            browser='Chrome', operating_system='Windows',
            device_type='Desktop', click_type='HUMAN')
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            api = self.client.get(
                '/api/campaigns/%d/report/link-clicks/' % self.campaign.id)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()['total_events'], 1)
        self.assertEqual(
            api.json()['events'][0]['short_url'],
            LOCAL_BASE + '/c/' + seeded.tracking_token)

    def test_shareable_report_derives(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            r = self.client.post(
                self.links_url,
                data=json.dumps(
                    {'name': 'Ad', 'destination_url': ODK_URL}),
                content_type='application/json')
        token = r.json()['tracking_token']
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            api = self.client.get(
                '/api/campaigns/%d/report/shareable-links/'
                % self.campaign.id)
        self.assertEqual(api.status_code, 200)
        by_name = {e['name']: e for e in api.json()['links']}
        self.assertEqual(
            by_name['Ad']['short_url'], LOCAL_BASE + '/c/' + token)

    def test_ordinary_tracked_anchor_derives(self):
        seeded = self._seed_recipient_row_under_prod()
        stored = ODK_URL.replace('&', '&amp;')
        html_in = (
            '<p><a href="https://example.com/page" data-original-url="'
            + stored + '" data-link-name="X">take it</a></p>'
        )
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            out = wrap_tracking(
                html_in, token_str='tok-9', track_opens=False,
                track_clicks=True, campaign=self.campaign,
                contact=self.contact)
        self.assertIn(
            'href="' + LOCAL_BASE + '/c/' + seeded.tracking_token + '"', out)
        self.assertNotIn(PROD_BASE, out)

    def test_token_identity_stable_across_base_change(self):
        with override_settings(SHORTENER_BASE_URL=PROD_BASE):
            html_prod = render_for_recipient(self.campaign, self.contact)
        first = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact, link_type=RECIPIENT)
        self.assertIn(PROD_BASE + '/c/' + first.tracking_token, html_prod)
        with override_settings(SHORTENER_BASE_URL=LOCAL_BASE):
            html_local = render_for_recipient(self.campaign, self.contact)
        second = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact, link_type=RECIPIENT)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.tracking_token, first.tracking_token)
        self.assertIn(LOCAL_BASE + '/c/' + first.tracking_token, html_local)

    def test_emission_points_derive_from_builder(self):
        from apps.campaigns import services as campaign_services
        from apps.reports import views as report_views
        from apps.tracking.serializers import (
            CampaignTrackingLinkSerializer)
        # Step 5 send path: no stored-URL emission, builder used.
        wrap_src = inspect.getsource(campaign_services.wrap_tracking)
        self.assertNotIn('recipient_link.short_url', wrap_src)
        self.assertIn('build_campaign_short_url', wrap_src)
        # Recipient click activity: first-party branch derives
        # (legacy recipient_link branch intentionally untouched).
        clicks_src = inspect.getsource(
            report_views.CampaignReportLinkClicksView.get)
        self.assertNotIn('.link.short_url', clicks_src)
        self.assertIn('e.recipient_link.short_url', clicks_src)
        # Shareable report derives.
        share_src = inspect.getsource(
            report_views.CampaignReportShareableLinksView.get)
        self.assertNotIn('l.short_url', share_src)
        # Step 1 serializer derives.
        self.assertIn(
            'build_campaign_short_url',
            inspect.getsource(
                CampaignTrackingLinkSerializer.get_short_url))
