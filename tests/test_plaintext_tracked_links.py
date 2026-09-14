import html as html_module
import json
import urllib.parse
from django.test import TestCase, Client
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.campaigns.services import wrap_tracking
from apps.sandbox.models import SandboxEmail
from apps.tracking.models import (
    CampaignTrackingLink, CampaignLinkClickEvent, EmailEvent,
)
from apps.tracking.utils import get_shortener_base_url

# Synthetic ODK-shaped URL (same structure/special chars as production URLs,
# but a fake host + token). Never use a real survey token in tests.
ODK_URL = (
    'https://odk.example.com/f/Ds8TwudfJFvhD0l7Pflj20oH'
    '?st=Ahg2TmSW5pDJXNt0Wog$zzxzqUUcxqBw2GE5cC!lxBMwb96gxKFVgbC4lv0FDknA&src=email&x=1'
)

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


def tracked_email_html(dest=ODK_URL):
    """Step 5 Insert URL with Track URL = ON (editor save format)."""
    stored = dest.replace('&', '&amp;')
    return (
        '<p>Hi</p><p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
        'data-original-url="' + stored + '" data-link-name="ODK Survey Link" '
        'data-track="true">Start Survey</a></p>'
    )


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


class PlaintextTrackedLinkTests(TestCase):
    """Step 5 Track URL = ON means first-party IRIS tracking rendered as
    plain text (no anchor), so providers receive body text they cannot
    link-rewrite. Track URL = OFF keeps the direct-link behavior."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='pt_admin', email='pt@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='PT Group')
        self.contact1 = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9101',
        )
        self.contact2 = Contact.objects.create(
            first_name='Sara', last_name='Ahmed', email='sara@example.com', job_id='9102',
        )
        self.contact1.groups.add(self.group)
        self.contact2.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='PT Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content=tracked_email_html(),
            destination_url=ODK_URL,
            track_opens=False,
            track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    def test_track_on_renders_bare_c_url_without_anchor(self):
        out = wrap_tracking(
            tracked_email_html(), token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertNotIn('{unique_link}', out)
        self.assertNotIn('sendibt3.com', out)
        self.assertNotIn('<a', out)
        self.assertNotIn('Start Survey', out)

        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        base = get_shortener_base_url().rstrip('/')
        self.assertTrue(link.short_url.startswith(base + '/c/'))
        self.assertIn(link.short_url, out)
        # Opaque token: nothing identifying the recipient leaks into the URL.
        self.assertEqual(len(link.tracking_token), 8)
        self.assertNotIn(self.contact1.email, link.short_url)
        self.assertNotIn('9101', link.tracking_token)
        # Destination stored server-side.
        self.assertEqual(link.destination_url, ODK_URL)
        self.assertEqual(link.shortened_link.original_url, ODK_URL)
        self.assertEqual(link.name, 'ODK Survey Link')

    def test_two_recipients_get_distinct_tokens(self):
        out1 = wrap_tracking(
            tracked_email_html(), token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
        out2 = wrap_tracking(
            tracked_email_html(), token_str='p2',
            campaign=self.campaign, contact=self.contact2,
        )
        link1 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        link2 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact2, link_type=RECIPIENT)
        self.assertNotEqual(link1.tracking_token, link2.tracking_token)
        self.assertIn(link1.short_url, out1)
        self.assertNotIn(link2.short_url, out1)
        self.assertIn(link2.short_url, out2)
        self.assertNotIn(link1.short_url, out2)

    def test_track_off_stays_direct_anchor(self):
        stored = ODK_URL.replace('&', '&amp;')
        direct = (
            '<p>Hi</p><p><a href="' + stored + '" data-track="false" '
            'target="_blank">Start Survey</a></p>'
        )
        out = wrap_tracking(
            direct, token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertIn('<a', out)
        self.assertIn(stored, out)
        self.assertNotIn('/c/', out)
        self.assertEqual(
            CampaignTrackingLink.objects.filter(
                campaign=self.campaign, link_type=RECIPIENT).count(), 0)

    def test_plain_anchor_without_datatrack_keeps_href_tracking(self):
        plain = '<p>Hi <a href="https://example.com/info">info page</a></p>'
        out = wrap_tracking(
            plain, token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        self.assertIn('href="' + link.short_url + '"', out)
        self.assertIn('info page', out)

    def test_click_mailed_bare_url_records_attributed_event(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email)
        out = wrap_tracking(
            tracked_email_html(), token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
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
        self.assertTrue(evt.browser)
        self.assertTrue(evt.operating_system)
        self.assertTrue(evt.device_type)
        self.assertEqual(evt.referrer, 'https://mail.example.com/')
        self.assertEqual(
            EmailEvent.objects.filter(
                event_type='CLICK', message=msg).count(), 1)

        link.refresh_from_db()
        self.assertEqual(link.human_click_count, 1)

        activity = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id).json()
        self.assertEqual(activity['total_events'], 1)
        self.assertEqual(activity['events'][0]['contact_email'], 'ali@example.com')
        self.assertNotIn('msg', activity['events'][0].get('contact_name', 'msg'))

    def test_preview_endpoint_renders_plain_text(self):
        resp = self.client.post(
            '/api/campaigns/%d/preview/' % self.campaign.id,
            data=json.dumps({
                'contact_id': self.contact1.id,
                'html_content': tracked_email_html(),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('<a', html)
        self.assertIn('/c/', html)

    def test_provider_payload_has_no_tracked_href(self):
        resp = self.client.post(
            '/api/campaigns/%d/test_email/' % self.campaign.id,
            data=json.dumps({'email': 'qa@example.com', 'contact_id': self.contact1.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        sent = SandboxEmail.objects.filter(
            to_email='qa@example.com').order_by('-created_at').first()
        self.assertIsNotNone(sent)
        self.assertNotIn('{unique_link}', sent.html_content)
        self.assertNotIn('sendibt3.com', sent.html_content)
        self.assertNotIn('<a', sent.html_content)
        self.assertIn('/c/', sent.html_content)

    def test_mailto_links_untouched(self):
        mailto = '<p>Write <a href="mailto:help@example.com">help</a></p>'
        out = wrap_tracking(
            mailto, token_str='p1',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertIn('<a href="mailto:help@example.com">help</a>', out)

    def test_modal_labels_first_party_tracking(self):
        page = self.client.get('/campaigns/create/')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('Track with IRIS', body)
        self.assertIn('tu-enable-tracking', body)
        self.assertIn('plain-text URL', body)
