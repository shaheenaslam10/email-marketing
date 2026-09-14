import html as html_module
import json
import urllib.parse
from django.test import TestCase, Client
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup, GroupCustomField, GroupContactValue
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.campaigns.services import (
    render_content_variables, wrap_tracking, expand_tracking_placeholders,
)
from apps.reminders.services import process_single_campaign_message
from apps.sandbox.models import SandboxEmail
from apps.tracking.models import (
    ShortenedLink, RecipientLink, LinkClickEvent,
    CampaignTrackingLink, CampaignLinkClickEvent,
)

# Synthetic ODK-shaped URL (same structure/special chars as production URLs,
# but a fake host + token). Never use a real survey token in tests.
ODK_URL = (
    'https://odk.example.com/f/Ds8TwudfJFvhD0l7Pflj20oH'
    '?st=Ahg2TmSW5pDJXNt0Wog$zzxzqUUcxqBw2GE5cC!lxBMwb96gxKFVgbC4lv0FDknA&src=email&x=1'
)
PERSONAL_URL = 'https://odk.example.com/f/Personal123?st=PersonalToken'

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT
PLACEHOLDER_HTML = '<p>Hi</p><p>{{survey_tracking_url}}</p>'


def extract_mailed_url(body, marker):
    """Extracts a bare-text URL containing marker from rendered mail HTML."""
    delims = '''"'<>()'''
    i = body.find(marker)
    start = i
    while start > 0 and (not body[start - 1].isspace()) and body[start - 1] not in delims:
        start -= 1
    end = i + len(marker)
    while end < len(body) and (not body[end].isspace()) and body[end] not in delims:
        end += 1
    return html_module.unescape(body[start:end])


def render_for(html, campaign, contact):
    """Mirrors the dispatch pipeline order: expand, substitute, wrap."""
    expanded = expand_tracking_placeholders(html, campaign, contact)
    rendered = render_content_variables(expanded, contact)
    return wrap_tracking(
        rendered, token_str='t1',
        campaign=campaign, contact=contact,
    )


class TrackingPlaceholderTests(TestCase):
    """Step 5 Insert Tracked URL inserts {{survey_tracking_url}}, which
    renders per recipient to a bare plain-text /c/ URL (no anchor)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='ph_admin', email='ph@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='PH Group')
        self.contact1 = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9201',
        )
        self.contact2 = Contact.objects.create(
            first_name='Sara', last_name='Ahmed', email='sara@example.com', job_id='9202',
        )
        self.contact1.groups.add(self.group)
        self.contact2.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='PH Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content=PLACEHOLDER_HTML,
            destination_url=ODK_URL,
            track_opens=False,
            track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    def _give_personal_url(self, contact, url=PERSONAL_URL):
        field = GroupCustomField.objects.create(
            group=self.group, name='Survey URL', slug='survey_url',
            field_type='URL', description=url)
        GroupContactValue.objects.create(contact=contact, field=field, value=url)

    def test_placeholder_expands_to_bare_c_url(self):
        out = render_for(PLACEHOLDER_HTML, self.campaign, self.contact1)
        self.assertNotIn('{{', out)
        self.assertNotIn('{unique_link}', out)
        self.assertNotIn('<a', out)
        self.assertNotIn('sendibt3.com', out)
        self.assertIn('/c/', out)

        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        self.assertIn(link.short_url, out)
        self.assertEqual(link.destination_url, ODK_URL)
        self.assertEqual(link.shortened_link.original_url, ODK_URL)
        self.assertEqual(link.name, 'Survey Tracking Link')
        self.assertEqual(len(link.tracking_token), 8)

    def test_personal_survey_url_wins_over_campaign_destination(self):
        self._give_personal_url(self.contact1)
        out = render_for(PLACEHOLDER_HTML, self.campaign, self.contact1)
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        self.assertEqual(link.destination_url, PERSONAL_URL)
        self.assertIn(link.short_url, out)

    def test_two_recipients_get_distinct_tokens(self):
        out1 = render_for(PLACEHOLDER_HTML, self.campaign, self.contact1)
        out2 = render_for(PLACEHOLDER_HTML, self.campaign, self.contact2)
        link1 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        link2 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact2, link_type=RECIPIENT)
        self.assertNotEqual(link1.tracking_token, link2.tracking_token)
        self.assertIn(link1.short_url, out1)
        self.assertNotIn(link2.short_url, out1)
        self.assertIn(link2.short_url, out2)
        self.assertNotIn(link1.short_url, out2)

    def test_no_destination_renders_empty_without_leak(self):
        bare = Campaign.objects.create(
            name='Bare', subject='Hi', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content=PLACEHOLDER_HTML,
            destination_url='', track_clicks=True,
        )
        out = render_for(PLACEHOLDER_HTML, bare, self.contact1)
        self.assertNotIn('{{', out)
        self.assertNotIn('{unique_link}', out)
        self.assertNotIn('/c/', out)
        self.assertEqual(
            CampaignTrackingLink.objects.filter(
                campaign=bare, link_type=RECIPIENT).count(), 0)

    def test_placeholder_inside_anchor_is_neutralized(self):
        nested = '<p><a href="https://example.com/x">go {{survey_tracking_url}}</a></p>'
        out = render_for(nested, self.campaign, self.contact1)
        self.assertNotIn('{{', out)
        self.assertNotIn('{unique_link}', out)
        self.assertNotIn('survey_tracking_url</a>', out)
        links = CampaignTrackingLink.objects.filter(
            campaign=self.campaign, link_type=RECIPIENT)
        self.assertEqual(links.count(), 1)
        self.assertEqual(links.first().destination_url, 'https://example.com/x')

    def test_click_mailed_url_full_chain(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.SENT,
        )
        out = render_for(PLACEHOLDER_HTML, self.campaign, self.contact1)
        mailed = extract_mailed_url(out, '/c/')
        parts = urllib.parse.urlparse(mailed)
        click = self.anon.get(
            parts.path, HTTP_USER_AGENT=HUMAN_UA,
            HTTP_REFERER='https://mail.example.com/')
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)

        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(evt.campaign, self.campaign)
        self.assertEqual(evt.contact, self.contact1)
        self.assertEqual(evt.click_type, 'HUMAN')
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)

        activity = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id).json()
        self.assertEqual(activity['total_events'], 1)
        self.assertEqual(activity['events'][0]['contact_email'], 'ali@example.com')

    def test_campaign_test_email_api_resolves_placeholder(self):
        resp = self.client.post(
            '/api/campaigns/%d/test_email/' % self.campaign.id,
            data=json.dumps({'email': 'qa@example.com', 'contact_id': self.contact1.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        sent = SandboxEmail.objects.filter(
            to_email='qa@example.com').order_by('-created_at').first()
        self.assertIsNotNone(sent)
        self.assertNotIn('{{', sent.html_content)
        self.assertNotIn('{unique_link}', sent.html_content)
        self.assertNotIn('<a', sent.html_content)
        self.assertIn('/c/', sent.html_content)

    def test_production_dispatch_resolves_placeholder(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        sent = SandboxEmail.objects.filter(
            to_email=self.contact1.email).order_by('-created_at').first()
        self.assertIsNotNone(sent)
        self.assertNotIn('{{', sent.html_content)
        self.assertNotIn('<a', sent.html_content)
        self.assertIn('/c/', sent.html_content)

    def test_preview_api_resolves_placeholder(self):
        resp = self.client.post(
            '/api/campaigns/%d/preview/' % self.campaign.id,
            data=json.dumps({
                'contact_id': self.contact1.id,
                'html_content': PLACEHOLDER_HTML,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{{', html)
        self.assertNotIn('<a', html)
        self.assertIn('/c/', html)

    def test_standalone_preview_without_campaign_renders_direct_plaintext(self):
        self._give_personal_url(self.contact1)
        resp = self.client.post(
            '/api/campaigns/render-preview/',
            data=json.dumps({
                'contact_id': self.contact1.id,
                'subject': 'Hi',
                'html_content': PLACEHOLDER_HTML,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{{', html)
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('<a', html)
        self.assertNotIn('/t/click/', html)
        self.assertIn(PERSONAL_URL, html)
        self.assertEqual(
            CampaignTrackingLink.objects.filter(link_type=RECIPIENT).count(), 0)

    def test_wizard_button_inserts_placeholder_directly(self):
        page = self.client.get('/campaigns/create/')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('onclick="insertSurveyTrackingPlaceholder()"', body)
        self.assertIn('function insertSurveyTrackingPlaceholder', body)
        self.assertIn('survey_tracking_url', body)
        self.assertIn('quill.insertText(index, token)', body)
        self.assertNotIn('onclick="openTrackedUrlModal(', body)

    def test_unknown_vars_still_wiped(self):
        self.assertEqual(
            render_content_variables('<p>{{nope_var}}</p>', self.contact1),
            '<p></p>')

    def test_campaign_without_contact_mints_shared_unattributed_c_link(self):
        tracked = (
            '<p>Hi</p><p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
            'data-original-url="' + ODK_URL.replace('&', '&amp;') + '" data-link-name="Survey" '
            'data-track="true">Take Survey</a></p>'
        )
        out1 = wrap_tracking(tracked, token_str='t1', campaign=self.campaign, contact=None)
        out2 = wrap_tracking(tracked, token_str='t2', campaign=self.campaign, contact=None)
        self.assertNotIn('/t/click/', out1)
        self.assertNotIn('<a', out1)
        self.assertIn('/c/', out1)
        links = CampaignTrackingLink.objects.filter(
            campaign=self.campaign, link_type=RECIPIENT)
        self.assertEqual(links.count(), 1)
        link = links.first()
        self.assertIsNone(link.contact)
        self.assertEqual(link.destination_url, ODK_URL)
        self.assertIn(link.short_url, out1)
        self.assertIn(link.short_url, out2)

        click = self.anon.get('/c/' + link.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        evt = CampaignLinkClickEvent.objects.get(link=link)
        self.assertIsNone(evt.contact)
        self.assertEqual(evt.campaign, self.campaign)
        activity = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id).json()
        self.assertEqual(activity['total_events'], 1)
        self.assertEqual(activity['events'][0]['contact_name'], '-')
        self.assertEqual(activity['events'][0]['short_url'], link.short_url)
        self.assertEqual(activity['events'][0]['destination_url'], ODK_URL)

    def test_ordinary_anchor_without_campaign_keeps_legacy_fallback(self):
        plain = '<p>Hi <a href="https://example.com/info">info page</a></p>'
        out = wrap_tracking(plain, token_str='t9', campaign=None, contact=None)
        self.assertIn('/t/click/', out)
        self.assertIn('<a', out)

    def test_link_clicks_api_returns_destination_url_both_branches(self):
        out = render_for(PLACEHOLDER_HTML, self.campaign, self.contact1)
        mailed = extract_mailed_url(out, '/c/')
        parts = urllib.parse.urlparse(mailed)
        self.anon.get(parts.path, HTTP_USER_AGENT=HUMAN_UA)
        sl = ShortenedLink.objects.create(
            campaign=self.campaign, original_url='https://example.com/legacy',
            link_name='Legacy Link')
        rl = RecipientLink.objects.create(
            shortened_link=sl, campaign=self.campaign, contact=self.contact2,
            tracking_token='LEGACY01',
            short_url='https://marketing.iriscommunications.cloud/r/LEGACY01')
        LinkClickEvent.objects.create(
            recipient_link=rl, campaign=self.campaign, contact=self.contact2,
            browser='Chrome', operating_system='Windows', device_type='Desktop',
            click_type='HUMAN')
        activity = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id).json()
        self.assertEqual(activity['total_events'], 2)
        by_email = {e['contact_email']: e for e in activity['events']}
        self.assertEqual(by_email['ali@example.com']['destination_url'], ODK_URL)
        self.assertEqual(
            by_email['sara@example.com']['destination_url'], 'https://example.com/legacy')
        self.assertIn('/c/', by_email['ali@example.com']['short_url'])

    def test_report_page_shows_tracking_url_and_destination_columns(self):
        page = self.client.get('/campaigns/%d/report/' % self.campaign.id)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('Tracking URL', body)
        self.assertIn('Destination', body)
        self.assertIn('e.destination_url', body)
