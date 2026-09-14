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
from apps.reminders.services import process_single_campaign_message
from apps.sandbox.models import SandboxEmail
from apps.tracking.models import (
    ShortenedLink, RecipientLink, LinkClickEvent, EmailEvent,
    CampaignTrackingLink, CampaignLinkClickEvent,
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
BOT_UA = 'Mozilla/5.0 (Windows NT 10.0) SafeLinks-Crawler'

RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT
SHAREABLE = CampaignTrackingLink.LinkType.SHAREABLE


def tracked_email_html(dest=ODK_URL):
    """Current editor save format: /c/ placeholder + data-* metadata."""
    stored = dest.replace('&', '&amp;')
    return (
        '<p>Hi</p><p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
        'data-original-url="' + stored + '" data-link-name="ODK Survey Link" '
        'data-track="true" target="_blank">Start Survey</a></p>'
    )


def extract_mailed_url(body, marker):
    """Extracts a bare-text URL containing marker from rendered mail HTML.

    First-party tracked links ship as plain text (no anchor element), so
    the mailed URL is delimited by whitespace or HTML punctuation.
    """
    delims = '''"'<>()'''
    i = body.find(marker)
    start = i
    while start > 0 and (not body[start - 1].isspace()) and body[start - 1] not in delims:
        start -= 1
    end = i + len(marker)
    while end < len(body) and (not body[end].isspace()) and body[end] not in delims:
        end += 1
    return html_module.unescape(body[start:end])


class RecipientCLinkTests(TestCase):
    """Acceptance Tests 1-7: branded /c/ recipient URLs."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='c_admin', email='cadmin@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='C Group')
        self.contact1 = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='8001',
        )
        self.contact2 = Contact.objects.create(
            first_name='Sara', last_name='Ahmed', email='sara@example.com', job_id='8002',
        )
        self.contact1.groups.add(self.group)
        self.contact2.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='C Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content=tracked_email_html(),
            destination_url=ODK_URL,
            track_opens=True,
            track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    # Test 1. Generated HTML contains the branded /c/ URL -------------------------
    def test_generated_html_uses_branded_c_url(self):
        out = wrap_tracking(
            tracked_email_html(), token_str='t1',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertNotIn('{unique_link}', out)
        self.assertNotIn('sendibt3.com', out)

        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)
        base = get_shortener_base_url().rstrip('/')
        self.assertTrue(link.short_url.startswith(base + '/c/'))
        self.assertIn(link.short_url, out)
        # Attribution + grouping stored server-side, never in the URL.
        self.assertNotIn(self.contact1.email, link.short_url)
        self.assertEqual(link.contact, self.contact1)
        self.assertEqual(link.destination_url, ODK_URL)
        self.assertEqual(link.shortened_link.original_url, ODK_URL)
        # Opaque token: 8 random alphanumerics, no sequence, no PII.
        self.assertEqual(len(link.tracking_token), 8)
        self.assertTrue(link.tracking_token.isalnum())

    # Test 2. A/B attribution --------------------------------------------------------
    def test_two_contacts_get_distinct_attributed_urls(self):
        out1 = wrap_tracking(
            tracked_email_html(), token_str='t1',
            campaign=self.campaign, contact=self.contact1,
        )
        out2 = wrap_tracking(
            tracked_email_html(), token_str='t2',
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
        # Same destination, shared ShortenedLink grouping.
        self.assertEqual(link1.shortened_link, link2.shortened_link)

        for link in (link1, link2):
            click = self.anon.get('/c/' + link.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
            self.assertEqual(click.status_code, 302)
            self.assertEqual(click.url, ODK_URL)
        ev1 = CampaignLinkClickEvent.objects.get(link=link1)
        ev2 = CampaignLinkClickEvent.objects.get(link=link2)
        self.assertEqual(ev1.contact, self.contact1)
        self.assertEqual(ev2.contact, self.contact2)

    # Test 3. /c/ redirect + recipient attribution --------------------------------------
    def test_c_click_redirects_and_attributes_recipient(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.SENT,
        )
        wrap_tracking(
            tracked_email_html(), token_str='t1',
            campaign=self.campaign, contact=self.contact1,
        )
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1, link_type=RECIPIENT)

        click = self.anon.get('/c/' + link.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        for fragment in ('?st=', '$', '!', '&src=email', '&x=1'):
            self.assertIn(fragment, click.url)

        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)
        self.assertEqual(link.human_click_count, 1)
        self.assertEqual(link.bot_click_count, 0)

        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)
        self.assertIsNotNone(msg.clicked_at)

        clk = EmailEvent.objects.get(message=msg, event_type=EmailEvent.EventType.CLICK)
        self.assertEqual(clk.url_clicked, ODK_URL)
        self.assertEqual(clk.metadata.get('link_name'), 'ODK Survey Link')
        self.assertEqual(clk.metadata.get('click_type'), 'HUMAN')

        # Bot click counts but never flips message status to CLICKED.
        msg2 = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact2,
            to_email=self.contact2.email, status=CampaignMessage.Status.SENT,
        )
        wrap_tracking(
            tracked_email_html(), token_str='t2',
            campaign=self.campaign, contact=self.contact2,
        )
        link2 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact2, link_type=RECIPIENT)
        bot = self.anon.get('/c/' + link2.tracking_token + '/', HTTP_USER_AGENT=BOT_UA)
        self.assertEqual(bot.status_code, 302)
        self.assertEqual(bot.url, ODK_URL)
        link2.refresh_from_db()
        self.assertEqual(link2.bot_click_count, 1)
        self.assertEqual(link2.human_click_count, 0)
        msg2.refresh_from_db()
        self.assertEqual(msg2.status, CampaignMessage.Status.SENT)
        self.assertIsNone(msg2.clicked_at)

    # Test 4. Shareable system intact ----------------------------------------------------
    def test_shareable_system_ignores_recipient_links(self):
        # Owner-created shareable link via the UI endpoint.
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/tracking-links/',
            data=json.dumps({'name': 'Facebook Ad'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        shareable = CampaignTrackingLink.objects.get(
            campaign=self.campaign, link_type=SHAREABLE)

        wrap_tracking(
            tracked_email_html(), token_str='t1',
            campaign=self.campaign, contact=self.contact1,
        )
        recipient = CampaignTrackingLink.objects.get(
            campaign=self.campaign, link_type=RECIPIENT)

        self.anon.get('/c/' + shareable.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.anon.get('/c/' + recipient.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)

        # Shareable report sees only the shareable link + its visit.
        rep = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/shareable-links/')
        self.assertEqual(rep.status_code, 200)
        data = rep.json()
        self.assertEqual(data['totals']['links'], 1)
        self.assertEqual(data['totals']['total_clicks'], 1)
        self.assertEqual([l['id'] for l in data['links']], [shareable.id])
        self.assertEqual(len(data['recent_events']), 1)

        # UI list/detail/regenerate exclude recipient links.
        listed = self.client.get(f'/api/campaigns/{self.campaign.id}/tracking-links/')
        self.assertEqual(
            [l['id'] for l in listed.json()['links']], [shareable.id])
        detail = self.client.get(
            f'/api/campaigns/{self.campaign.id}/tracking-links/{recipient.id}/')
        self.assertEqual(detail.status_code, 404)
        regen = self.client.post(
            f'/api/campaigns/{self.campaign.id}/tracking-links/{recipient.id}/regenerate/')
        self.assertEqual(regen.status_code, 404)

        # Recipient report sees only the recipient click, not the shareable visit.
        recip = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=all')
        rows = {r['email']: r for r in recip.json()['recipients']}
        self.assertEqual(rows['ali@example.com']['total_clicks'], 1)
        self.assertIn('/c/', rows['ali@example.com']['short_url'])

    # Test 5. Recipient analytics union ---------------------------------------------------
    def test_recipient_analytics_union_legacy_and_c(self):
        # Legacy source: root-level RecipientLink row + event (old delivery).
        sl = ShortenedLink.objects.create(
            campaign=self.campaign, original_url=ODK_URL, link_name='ODK Survey Link')
        legacy = RecipientLink.objects.create(
            shortened_link=sl, campaign=self.campaign, contact=self.contact1,
            tracking_token='LEGACY1', short_url='https://marketing.iriscommunications.cloud/LEGACY1',
            click_count=1, human_click_count=1,
        )
        LinkClickEvent.objects.create(
            recipient_link=legacy, campaign=self.campaign, contact=self.contact1,
            browser='Chrome', device_type='Desktop',
            click_type=LinkClickEvent.ClickType.HUMAN,
        )
        # New source: /c/ recipient link + visit (same destination reuses sl).
        wrap_tracking(
            tracked_email_html(), token_str='t2',
            campaign=self.campaign, contact=self.contact2,
        )
        link2 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact2, link_type=RECIPIENT)
        self.assertEqual(link2.shortened_link, sl)
        self.anon.get('/c/' + link2.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)

        full = self.client.get(f'/api/campaigns/{self.campaign.id}/report/')
        self.assertEqual(full.status_code, 200)
        data = full.json()
        summary = data['link_summary']
        self.assertEqual(summary['total_link_clicks'], 2)
        self.assertEqual(summary['human_clicks'], 2)
        self.assertEqual(summary['bot_clicks'], 0)
        self.assertEqual(summary['unique_link_clicks'], 2)
        perf = {p['id']: p for p in data['link_performance']}
        self.assertEqual(perf[sl.id]['total_clicks'], 2)
        self.assertEqual(perf[sl.id]['unique_clicks'], 2)
        self.assertEqual(data['device_analytics']['Desktop'], 2)
        self.assertEqual(data['browser_analytics']['Chrome'], 2)
        self.assertEqual(sum(d['clicks'] for d in data['click_timeline']), 2)

        recip = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=clicked')
        rows = {r['email']: r for r in recip.json()['recipients']}
        self.assertEqual(rows['ali@example.com']['total_clicks'], 1)
        self.assertEqual(rows['sara@example.com']['total_clicks'], 1)

        for contact in (self.contact1, self.contact2):
            timeline = self.client.get(f'/api/contacts/{contact.id}/timeline/')
            self.assertEqual(timeline.status_code, 200)
            types = [e['type'] for e in timeline.json()]
            self.assertIn('LINK_CLICK', types)

    # Test 6. Invalid token -------------------------------------------------------------------
    def test_invalid_c_token_returns_404_without_side_effects(self):
        resp = self.anon.get('/c/NOPE1234/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)
        self.assertEqual(EmailEvent.objects.count(), 0)

    # Test 7. Delivered-mail body carries the branded URL --------------------------------------
    def test_delivered_mail_body_contains_branded_c_url(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        sent = SandboxEmail.objects.filter(
            to_email=self.contact1.email).order_by('-created_at').first()
        self.assertIsNotNone(sent)
        html = sent.html_content
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('sendibt3.com', html)
        self.assertIn('/c/', html)
        self.assertNotIn('<a', html)

        # The mailed URL must be directly clickable to the exact destination.
        mailed = extract_mailed_url(html, '/c/')
        parts = urllib.parse.urlparse(mailed)
        path = parts.path + ('?' + parts.query if parts.query else '')
        click = self.anon.get(path, HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)

    # Editor modal renders the /c/ placeholder -------------------------------------------------
    def test_editor_modal_shows_c_placeholder(self):
        page = self.client.get(f'/campaigns/{self.campaign.id}/edit/?mode=draft')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('/c/{unique_link}', body)
        base = get_shortener_base_url().rstrip('/')
        self.assertIn(base + '/c/{unique_link}', body)
