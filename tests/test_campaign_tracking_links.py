import json
from django.test import TestCase, Client
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.tracking.models import (
    CampaignTrackingLink, CampaignLinkClickEvent, EmailEvent, TrackingToken,
)
from apps.tracking.utils import (
    generate_unique_tracking_token, build_campaign_short_url,
    append_query_params,
)

HUMAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BOT_UA = "Mozilla/5.0 (Windows NT 10.0) SafeLinks-Crawler"


class CampaignTrackingLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='tracker_admin', email='tracker@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()  # unauthenticated browser

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='Track Group')
        self.contact = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9001',
        )
        self.contact.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='Tracked Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>Hello</p>',
            destination_url='https://www.odk.iriscommunications.cloud/project/1/forms/reg',
        )
        self.campaign.groups.add(self.group)

    def _make_link(self, **kwargs):
        kwargs.setdefault('campaign', self.campaign)
        token = generate_unique_tracking_token(CampaignTrackingLink, length=8)
        return CampaignTrackingLink.objects.create(
            tracking_token=token,
            short_url=build_campaign_short_url(token),
            **kwargs
        )

    # 1. Tracking URL generation -------------------------------------------
    def test_api_generate_tracking_link(self):
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/tracking-links/',
            data=json.dumps({'name': 'Facebook Ad'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(len(data['tracking_token']), 8)
        self.assertTrue(data['tracking_token'].isalnum())
        self.assertIn('/c/', data['short_url'])
        self.assertTrue(data['is_active'])
        self.assertEqual(data['name'], 'Facebook Ad')

    def test_api_generate_requires_auth(self):
        resp = self.anon.post(
            f'/api/campaigns/{self.campaign.id}/tracking-links/',
            data=json.dumps({'name': 'X'}),
            content_type='application/json',
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_generated_tokens_are_unique(self):
        tokens = {
            generate_unique_tracking_token(CampaignTrackingLink, length=8)
            for _ in range(50)
        }
        self.assertEqual(len(tokens), 50)

    def test_api_list_update_regenerate_delete(self):
        link = self._make_link(name='v1')
        # list
        resp = self.client.get(f'/api/campaigns/{self.campaign.id}/tracking-links/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['links']), 1)
        # update label + disable
        resp = self.client.patch(
            f'/api/campaigns/{self.campaign.id}/tracking-links/{link.id}/',
            data=json.dumps({'name': 'v2', 'is_active': False}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        link.refresh_from_db()
        self.assertEqual(link.name, 'v2')
        self.assertFalse(link.is_active)
        # regenerate issues a fresh token/URL
        old_token = link.tracking_token
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/tracking-links/{link.id}/regenerate/'
        )
        self.assertEqual(resp.status_code, 200)
        link.refresh_from_db()
        self.assertNotEqual(link.tracking_token, old_token)
        self.assertIn(link.tracking_token, link.short_url)
        # delete
        resp = self.client.delete(
            f'/api/campaigns/{self.campaign.id}/tracking-links/{link.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(CampaignTrackingLink.objects.filter(pk=link.pk).exists())

    def test_link_detail_scoped_to_campaign(self):
        other = Campaign.objects.create(
            name='Other', subject='s', sender=self.sender,
            status=Campaign.Status.ACTIVE, html_content='<p>x</p>',
        )
        link = self._make_link()
        resp = self.client.get(f'/api/campaigns/{other.id}/tracking-links/{link.id}/')
        self.assertEqual(resp.status_code, 404)

    # 2/4/5/6. Valid redirect + event + association + destination ------------
    def test_valid_redirect_records_event_and_redirects(self):
        link = self._make_link(name='Ad')
        resp = self.anon.get(
            f'/c/{link.tracking_token}/',
            HTTP_USER_AGENT=HUMAN_UA,
            HTTP_X_FORWARDED_FOR='203.0.113.195',
            HTTP_REFERER='https://facebook.com/ad',
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, self.campaign.destination_url)

        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)
        self.assertEqual(link.human_click_count, 1)
        self.assertIsNotNone(link.first_clicked_at)
        self.assertIsNotNone(link.last_clicked_at)

        event = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(event.campaign, self.campaign)
        self.assertEqual(event.ip_address, '203.0.113.195')
        self.assertEqual(event.browser, 'Chrome')
        self.assertEqual(event.device_type, 'Desktop')
        self.assertEqual(event.operating_system, 'Windows')
        self.assertEqual(event.referrer, 'https://facebook.com/ad')
        self.assertEqual(event.click_type, 'HUMAN')

    def test_link_destination_overrides_campaign_default(self):
        link = self._make_link(destination_url='https://example.com/special')
        resp = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, 'https://example.com/special')

    def test_utm_query_params_pass_through(self):
        link = self._make_link()
        resp = self.anon.get(
            f'/c/{link.tracking_token}/?utm_source=fb&utm_medium=cpc',
            HTTP_USER_AGENT=HUMAN_UA,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('utm_source=fb', resp.url)
        self.assertTrue(resp.url.startswith(self.campaign.destination_url))

    def test_no_contact_or_email_event_created_for_anonymous_click(self):
        """Anonymous link clicks must not fabricate contact associations."""
        link = self._make_link()
        self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(EmailEvent.objects.count(), 0)

    # 3. Invalid token -------------------------------------------------------
    def test_invalid_token_returns_404(self):
        resp = self.anon.get('/c/NOPE1234/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    # 9. Multiple visits ------------------------------------------------------
    def test_multiple_visits_and_bot_classification(self):
        link = self._make_link()
        self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=BOT_UA)
        link.refresh_from_db()
        self.assertEqual(link.click_count, 3)
        self.assertEqual(link.human_click_count, 2)
        self.assertEqual(link.bot_click_count, 1)
        self.assertEqual(
            CampaignLinkClickEvent.objects.filter(link=link).count(), 3
        )
        self.assertEqual(
            CampaignLinkClickEvent.objects.filter(
                link=link, click_type='SUSPECTED_BOT'
            ).count(), 1
        )

    # 10/11. Anonymous + authenticated -----------------------------------------
    def test_redirect_works_without_login_and_with_login(self):
        link = self._make_link()
        r1 = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        r2 = self.client.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r2.status_code, 302)

    # 12/13. Malicious destinations + open redirect ------------------------------
    def test_api_rejects_malicious_destination(self):
        for bad in ('javascript:alert(1)', 'data:text/html,x', 'ftp://x.com/f',
                    'https://evil.com\r\nX: y', 'not-a-url'):
            resp = self.client.post(
                f'/api/campaigns/{self.campaign.id}/tracking-links/',
                data=json.dumps({'destination_url': bad}),
                content_type='application/json',
            )
            self.assertEqual(resp.status_code, 400, f'should reject {bad!r}')

    def test_campaign_destination_validation(self):
        resp = self.client.patch(
            f'/api/campaigns/{self.campaign.id}/',
            data=json.dumps({'destination_url': 'javascript:alert(1)'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.patch(
            f'/api/campaigns/{self.campaign.id}/',
            data=json.dumps({'destination_url': 'https://safe.example.com/done'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.destination_url, 'https://safe.example.com/done')

    def test_redirect_refuses_unconfigured_or_unsafe_destination(self):
        self.campaign.destination_url = ''
        self.campaign.save(update_fields=['destination_url'])
        link = self._make_link()
        resp = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_legacy_click_endpoint_blocks_unsafe_url(self):
        msg = CampaignMessage.objects.create(
            campaign=self.campaign, contact=self.contact,
            to_email=self.contact.email, status=CampaignMessage.Status.SENT,
        )
        token = TrackingToken.objects.create(message=msg)
        resp = self.anon.get(f'/t/click/{token.token}/?url=javascript:alert(1)')
        self.assertEqual(resp.status_code, 400)
        # safe absolute URL still redirects and tracks
        resp = self.anon.get(
            f'/t/click/{token.token}/?url=https://safe.example.com/x',
            HTTP_USER_AGENT=HUMAN_UA,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, 'https://safe.example.com/x')

    # 14. Disabled / inactive ----------------------------------------------------
    def test_disabled_link_returns_410_and_tracks_nothing(self):
        link = self._make_link(is_active=False)
        resp = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_paused_or_cancelled_campaign_disables_link(self):
        link = self._make_link()
        for st in (Campaign.Status.PAUSED, Campaign.Status.CANCELLED):
            self.campaign.status = st
            self.campaign.save(update_fields=['status'])
            resp = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
            self.assertEqual(resp.status_code, 410, f'status {st} should disable')
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_draft_campaign_link_still_redirects_for_owner_testing(self):
        self.campaign.status = Campaign.Status.DRAFT
        self.campaign.save(update_fields=['status'])
        link = self._make_link()
        resp = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 302)

    # 15. Existing behavior without tracking --------------------------------------
    def test_campaign_without_links_behaves_normally(self):
        resp = self.client.get(f'/api/campaigns/{self.campaign.id}/tracking-links/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['links'], [])
        # duplicate carries destination_url but not links
        dup = self.client.post(f'/api/campaigns/{self.campaign.id}/duplicate/')
        self.assertEqual(dup.status_code, 201)
        new_id = dup.json()['campaign']['id']
        new_campaign = Campaign.objects.get(pk=new_id)
        self.assertEqual(new_campaign.destination_url, self.campaign.destination_url)
        self.assertEqual(new_campaign.tracking_links.count(), 0)

    def test_existing_short_url_flow_unaffected(self):
        """Root short-URL route and /c/ route coexist without collisions."""
        from apps.tracking.models import ShortenedLink, RecipientLink
        sl = ShortenedLink.objects.create(
            campaign=self.campaign, original_url='https://odk.example.com/f',
            link_name='ODK',
        )
        rl = RecipientLink.objects.create(
            shortened_link=sl, campaign=self.campaign, contact=self.contact,
            tracking_token='AbC123Xy', short_url='https://marketing.iriscommunications.cloud/AbC123Xy',
        )
        r1 = self.anon.get('/AbC123Xy/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r1.url, 'https://odk.example.com/f')
        link = self._make_link()
        r2 = self.anon.get(f'/c/{link.tracking_token}/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(r2.status_code, 302)

    def test_append_query_params_helper(self):
        self.assertEqual(
            append_query_params('https://x.com/a', 'u=1'),
            'https://x.com/a?u=1',
        )
        self.assertEqual(
            append_query_params('https://x.com/a?b=2', 'u=1'),
            'https://x.com/a?b=2&u=1',
        )
        self.assertEqual(append_query_params('https://x.com/a', ''), 'https://x.com/a')
