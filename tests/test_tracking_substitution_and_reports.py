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
from apps.tracking.utils import generate_unique_tracking_token, build_campaign_short_url

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


def tracked_email_html(dest=ODK_URL, enabled=True):
    """Post-fix editor save format: data-* preserved, & stored as &amp;."""
    stored = dest.replace('&', '&amp;')
    if enabled:
        return (
            '<p>Hi</p><p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
            'data-original-url="' + stored + '" data-link-name="ODK Survey Link" '
            'data-track="true" target="_blank">Start Survey</a></p>'
        )
    return (
        '<p>Hi</p><p><a href="' + stored + '" data-track="false" '
        'target="_blank">Start Survey</a></p>'
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


class TrackingSubstitutionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='subst_admin', email='subst@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='Subst Group')
        self.contact1 = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='7001',
        )
        self.contact2 = Contact.objects.create(
            first_name='Sara', last_name='Ahmed', email='sara@example.com', job_id='7002',
        )
        self.contact1.groups.add(self.group)
        self.contact2.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='Subst Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content=tracked_email_html(enabled=True),
            destination_url=ODK_URL,
            track_opens=True,
            track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    def _latest_sent_html(self, to_email):
        sent = SandboxEmail.objects.filter(to_email=to_email).order_by('-created_at').first()
        self.assertIsNotNone(sent, 'expected a captured outbound email for %s' % to_email)
        return sent.html_content

    # 1. Test email with tracking enabled ------------------------------------
    def test_test_email_enabled_produces_working_tracking_url(self):
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/test_email/',
            data=json.dumps({'email': 'qa@example.com', 'contact_id': self.contact1.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = self._latest_sent_html('qa@example.com')
        self.assertNotIn('{unique_link}', html)

        rl = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertEqual(rl.link_type, CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertEqual(rl.contact, self.contact1)
        self.assertIn('/c/', rl.short_url)
        self.assertIn(rl.short_url, html)

        click = self.anon.get('/c/' + rl.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        self.assertEqual(CampaignLinkClickEvent.objects.filter(link=rl).count(), 1)

    def test_standalone_test_email_uses_direct_plaintext(self):
        resp = self.client.post(
            '/api/campaigns/test_email/',
            data=json.dumps({
                'email': 'qa2@example.com',
                'subject': 'Standalone',
                'html_content': tracked_email_html(enabled=True),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = self._latest_sent_html('qa2@example.com')
        self.assertNotIn('{unique_link}', html)
        # No campaign exists, so no /c/ row can be minted; tracked links
        # degrade to the direct destination as plain text (never the
        # legacy /t/click/?url= fallback, which would expose it).
        self.assertNotIn('/t/click/', html)
        self.assertNotIn('<a', html)
        self.assertIn(ODK_URL, html_module.unescape(html))
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)
        self.assertEqual(ShortenedLink.objects.count(), 0)

    # 2. Test email with tracking disabled ------------------------------------
    def test_test_email_disabled_stays_direct(self):
        self.campaign.html_content = tracked_email_html(enabled=False)
        self.campaign.save(update_fields=['html_content'])
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/test_email/',
            data=json.dumps({'email': 'qa@example.com', 'contact_id': self.contact1.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = self._latest_sent_html('qa@example.com')
        self.assertNotIn('{unique_link}', html)
        self.assertIn(ODK_URL, html_module.unescape(html))
        self.assertNotIn('/t/click/', html)
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)
        self.assertEqual(ShortenedLink.objects.count(), 0)

    # 3. Real recipient send with tracking enabled -----------------------------
    def test_real_send_enabled_tracks_recipient(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        html = self._latest_sent_html(self.contact1.email)
        self.assertNotIn('{unique_link}', html)

        rl = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertIn(rl.short_url, html)

        click = self.anon.get('/c/' + rl.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)

    # 4. Tracking disabled stays direct -----------------------------------------
    def test_real_send_campaign_tracking_off_resolves_direct(self):
        self.campaign.track_clicks = False
        self.campaign.save(update_fields=['track_clicks'])
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        html = self._latest_sent_html(self.contact1.email)
        self.assertNotIn('{unique_link}', html)
        self.assertIn(ODK_URL, html_module.unescape(html))
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)
        self.assertEqual(ShortenedLink.objects.count(), 0)

    def test_real_send_link_tracking_off_stays_direct(self):
        self.campaign.html_content = tracked_email_html(enabled=False)
        self.campaign.save(update_fields=['html_content'])
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        html = self._latest_sent_html(self.contact1.email)
        self.assertNotIn('{unique_link}', html)
        self.assertIn(ODK_URL, html_module.unescape(html))
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)

    # 5. Editor metadata preservation --------------------------------------------
    def test_editor_registers_tracking_link_blot(self):
        resp = self.client.get(f'/campaigns/{self.campaign.id}/edit/?mode=draft')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')
        self.assertIn('TrackedLink', html)
        self.assertIn('data-original-url', html)
        self.assertIn('Quill.register(TrackedLink', html)

    def test_legacy_bare_placeholder_left_untouched(self):
        # Content saved before the editor preserved metadata has no
        # data-original-url, so the destination is unknowable: it is left
        # as-is (owner must re-insert the link via the modal).
        legacy = '<p><a href="https://marketing.iriscommunications.cloud/{unique_link}">x</a></p>'
        out = wrap_tracking(legacy, token_str='tok', campaign=self.campaign, contact=self.contact1)
        self.assertIn('{unique_link}', out)
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)

    # 6/7. Substitution + recipient-specific URLs ----------------------------------
    def test_unique_link_substitution(self):
        out = wrap_tracking(
            tracked_email_html(enabled=True), token_str='tok',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertNotIn('{unique_link}', out)
        rl = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertIn(rl.short_url, out)

        out_disabled = wrap_tracking(
            tracked_email_html(enabled=False), token_str='tok',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertNotIn('{unique_link}', out_disabled)
        self.assertIn(ODK_URL, html_module.unescape(out_disabled))

    def test_recipient_specific_urls_per_contact(self):
        out1 = wrap_tracking(
            tracked_email_html(enabled=True), token_str='t1',
            campaign=self.campaign, contact=self.contact1,
        )
        out2 = wrap_tracking(
            tracked_email_html(enabled=True), token_str='t2',
            campaign=self.campaign, contact=self.contact2,
        )
        rl1 = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        rl2 = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact2, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertNotEqual(rl1.tracking_token, rl2.tracking_token)
        self.assertIn(rl1.short_url, out1)
        self.assertIn(rl2.short_url, out2)
        for rl in (rl1, rl2):
            click = self.anon.get('/c/' + rl.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
            self.assertEqual(click.status_code, 302)
            self.assertEqual(click.url, ODK_URL)

    # 8/9. Redirect + query preservation --------------------------------------------
    def test_redirect_opens_exact_odk_url(self):
        wrap_tracking(
            tracked_email_html(enabled=True), token_str='tok',
            campaign=self.campaign, contact=self.contact1,
        )
        rl = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        click = self.anon.get('/c/' + rl.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        for fragment in ('?st=', '$', '!', '&src=email', '&x=1'):
            self.assertIn(fragment, click.url)

    # 10. & / &amp; handling ----------------------------------------------------------
    def test_ampersand_entity_round_trip(self):
        wrap_tracking(
            tracked_email_html(enabled=True), token_str='tok',
            campaign=self.campaign, contact=self.contact1,
        )
        sl = ShortenedLink.objects.get(campaign=self.campaign)
        self.assertEqual(sl.original_url, ODK_URL)
        self.assertNotIn('&amp;', sl.original_url)
        self.assertEqual(sl.link_name, 'ODK Survey Link')
        # Re-wrap must reuse the same rows (dedup on clean URL).
        wrap_tracking(
            tracked_email_html(enabled=True), token_str='tok',
            campaign=self.campaign, contact=self.contact1,
        )
        self.assertEqual(ShortenedLink.objects.filter(campaign=self.campaign).count(), 1)
        self.assertEqual(CampaignTrackingLink.objects.filter(campaign=self.campaign, link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 1)

    # 15. No double tracking ------------------------------------------------------------
    def test_no_double_tracking(self):
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/test_email/',
            data=json.dumps({'email': 'qa@example.com', 'contact_id': self.contact1.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        first = CampaignTrackingLink.objects.get(campaign=self.campaign, contact=self.contact1, link_type=CampaignTrackingLink.LinkType.RECIPIENT)

        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        # Real dispatch reuses the same recipient link: no duplicates.
        self.assertEqual(CampaignTrackingLink.objects.filter(campaign=self.campaign, link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 1)
        self.assertEqual(
            CampaignTrackingLink.objects.get(campaign=self.campaign, link_type=CampaignTrackingLink.LinkType.RECIPIENT).tracking_token,
            first.tracking_token,
        )
        # One click produces exactly one click event.
        self.anon.get('/c/' + first.tracking_token + '/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(
            CampaignLinkClickEvent.objects.filter(link=first).count(), 1
        )

    # 16. Campaigns without tracking ------------------------------------------------------
    def test_campaign_without_tracking_unaffected(self):
        self.campaign.track_clicks = False
        self.campaign.track_opens = False
        self.campaign.html_content = (
            '<p>Hi <a href="https://example.com/info?a=1&b=2">info</a></p>'
        )
        self.campaign.save(update_fields=['track_clicks', 'track_opens', 'html_content'])
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact1,
            to_email=self.contact1.email, status=CampaignMessage.Status.QUEUED,
        )
        self.assertTrue(process_single_campaign_message(msg.id))
        html = self._latest_sent_html(self.contact1.email)
        self.assertNotIn('/t/open/', html)
        self.assertIn('a=1&b=2', html_module.unescape(html))
        self.assertEqual(CampaignTrackingLink.objects.filter(link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 0)
        self.assertEqual(ShortenedLink.objects.count(), 0)


class ShareableReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='report_admin', email='report@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.campaign = Campaign.objects.create(
            name='Report Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>Hello</p>',
            destination_url=ODK_URL,
        )

    def _make_link(self, **kwargs):
        kwargs.setdefault('campaign', self.campaign)
        token = generate_unique_tracking_token(CampaignTrackingLink, length=8)
        return CampaignTrackingLink.objects.create(
            tracking_token=token,
            short_url=build_campaign_short_url(token),
            **kwargs
        )

    def _click(self, link, ua=HUMAN_UA, referrer=None):
        extra = {'HTTP_USER_AGENT': ua}
        if referrer:
            extra['HTTP_REFERER'] = referrer
        return self.anon.get('/c/' + link.tracking_token + '/', **extra)

    # 11. Shareable URL with query destination ----------------------------------------------
    def test_shareable_link_redirects_exact_odk_url(self):
        link = self._make_link(name='Ad')
        click = self._click(link)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)

        tracked = self._click(link, referrer='https://ads.example.com/')
        self.assertEqual(tracked.status_code, 302)
        utm = self.anon.get(
            '/c/' + link.tracking_token + '/?utm_source=fb', HTTP_USER_AGENT=HUMAN_UA
        )
        self.assertEqual(utm.status_code, 302)
        self.assertEqual(utm.url, ODK_URL + '&utm_source=fb')

    # 12. Click event recording -----------------------------------------------------------------
    def test_shareable_click_events_recorded(self):
        link = self._make_link(name='Ad')
        self._click(link)
        self._click(link, referrer='https://facebook.com/ad')
        self._click(link, ua=BOT_UA)
        link.refresh_from_db()
        self.assertEqual(link.click_count, 3)
        self.assertEqual(link.human_click_count, 2)
        self.assertEqual(link.bot_click_count, 1)

        events = CampaignLinkClickEvent.objects.filter(link=link).order_by('clicked_at')
        self.assertEqual(events.count(), 3)
        self.assertTrue(all(e.campaign == self.campaign for e in events))
        self.assertEqual(events[1].referrer, 'https://facebook.com/ad')
        self.assertEqual(events[2].click_type, 'SUSPECTED_BOT')
        # Anonymous system: no contact/email linkage may be fabricated.
        self.assertTrue(all(e.contact is None for e in events))
        self.assertEqual(EmailEvent.objects.count(), 0)
        self.assertEqual(LinkClickEvent.objects.count(), 0)

    # 13. Shareable Link Activity report -------------------------------------------------------------
    def test_shareable_report_api(self):
        link_a = self._make_link(name='Facebook Ad', destination_url='https://example.com/alt')
        link_b = self._make_link(name='QR Flyer')
        self._click(link_a, referrer='https://ads.example.com/')
        self._click(link_a)
        self._click(link_b, ua=BOT_UA)
        link_b.is_active = False
        link_b.save(update_fields=['is_active'])

        resp = self.client.get(f'/api/campaigns/{self.campaign.id}/report/shareable-links/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data['totals']['links'], 2)
        self.assertEqual(data['totals']['active_links'], 1)
        self.assertEqual(data['totals']['total_clicks'], 3)
        self.assertEqual(data['totals']['human_clicks'], 2)
        self.assertEqual(data['totals']['bot_clicks'], 1)

        by_id = {l['id']: l for l in data['links']}
        self.assertEqual(by_id[link_a.id]['destination_url'], 'https://example.com/alt')
        self.assertFalse(by_id[link_a.id]['uses_campaign_default'])
        self.assertEqual(by_id[link_b.id]['destination_url'], ODK_URL)
        self.assertTrue(by_id[link_b.id]['uses_campaign_default'])
        self.assertFalse(by_id[link_b.id]['is_active'])

        events = data['recent_events']
        self.assertEqual(len(events), 3)
        # Newest first.
        self.assertEqual(events[0]['click_type'], 'Suspected Bot')
        self.assertEqual(events[1]['click_type'], 'Human')
        self.assertEqual(events[1]['browser'], 'Chrome')
        self.assertEqual(events[1]['device_type'], 'Desktop')
        self.assertEqual(events[2]['referrer'], 'https://ads.example.com/')
        # IPs must stay out of the API (privacy).
        self.assertNotIn('ip_address', json.dumps(events))

        # Link filter + limit.
        filtered = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/shareable-links/?link_id={link_a.id}'
        )
        self.assertEqual(len(filtered.json()['recent_events']), 2)
        limited = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/shareable-links/?limit=1'
        )
        self.assertEqual(len(limited.json()['recent_events']), 1)

    def test_shareable_report_requires_auth_and_page_exists(self):
        resp = self.anon.get(f'/api/campaigns/{self.campaign.id}/report/shareable-links/')
        self.assertIn(resp.status_code, (401, 403))
        page = self.client.get(f'/campaigns/{self.campaign.id}/report/')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('Shareable Link Activity', body)
        self.assertIn('Shareable / Anonymous', body)

    # 14. Existing recipient report keeps working -------------------------------------------------------
    def test_existing_recipient_report_excludes_shareable(self):
        link = self._make_link(name='Ad')
        self._click(link)
        self._click(link, ua=BOT_UA)

        recip = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=all'
        )
        self.assertEqual(recip.status_code, 200)
        self.assertEqual(
            sum(r['total_clicks'] for r in recip.json()['recipients']), 0
        )
        full = self.client.get(f'/api/campaigns/{self.campaign.id}/report/')
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.json()['link_summary']['total_link_clicks'], 0)
        self.assertEqual(full.json()['link_performance'], [])
