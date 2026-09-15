import json
from unittest.mock import MagicMock, patch
from django.test import TestCase, Client
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign
from apps.email_providers.providers import (
    BrevoProvider, MailgunProvider, SendGridProvider, PostmarkProvider,
)
from apps.tracking.models import CampaignTrackingLink
from apps.tracking.utils import get_shortener_base_url

# Synthetic ODK-shaped URL (same structure/special chars as production URLs,
# but a fake host + token). Never use a real survey token in tests.
ODK_URL = (
    'https://odk.example.com/f/Ds8TwudfJFvhD0l7Pflj20oH'
    '?st=Ahg2TmSW5pDJXNt0Wog$zzxzqUUcxqBw2GE5cC!lxBMwb96gxKFVgbC4lv0FDknA&src=email&x=1'
)

RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


def tracked_email_html(dest=ODK_URL):
    """Current editor save format: /c/ placeholder + data-* metadata."""
    stored = dest.replace('&', '&amp;')
    return (
        '<p>Hi</p><p><a href="https://marketing.iriscommunications.cloud/c/{unique_link}" '
        'data-original-url="' + stored + '" data-link-name="ODK Survey Link" '
        'data-track="true" target="_blank">Start Survey</a></p>'
    )


def make_sender(provider_type, flag=False):
    sender = Sender.objects.create(
        name='Team',
        email='survey@marketing.iriscommunications.cloud',
        provider_type=provider_type,
        disable_provider_click_tracking=flag,
    )
    sender.password_or_key = 'test-secret'
    sender.save(update_fields=['password_or_key_encrypted'])
    return sender


def fake_response(status_code, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    return resp


class PreviewEndpointsTests(TestCase):
    """Problem 2: preview must show resolved branded URLs, never {unique_link}."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='pv_admin', email='pv@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='PV Group')
        self.contact = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9001',
        )
        self.contact.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='PV Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content=tracked_email_html(),
            destination_url=ODK_URL,
            track_opens=True,
            track_clicks=True,
        )
        self.campaign.groups.add(self.group)

    def test_campaign_preview_resolves_branded_c_url(self):
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/preview/',
            data=json.dumps({
                'contact_id': self.contact.id,
                'html_content': tracked_email_html(),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('%7Bunique_link%7D', html)

        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact, link_type=RECIPIENT)
        base = get_shortener_base_url().rstrip('/')
        self.assertTrue(link.short_url.startswith(base + '/c/'))
        self.assertIn(link.short_url, html)

    def test_campaign_preview_reuses_link_rows(self):
        url = f'/api/campaigns/{self.campaign.id}/preview/'
        body = json.dumps({
            'contact_id': self.contact.id,
            'html_content': tracked_email_html(),
        })
        self.client.post(url, data=body, content_type='application/json')
        self.client.post(url, data=body, content_type='application/json')
        self.assertEqual(
            CampaignTrackingLink.objects.filter(
                campaign=self.campaign, link_type=RECIPIENT).count(), 1)

    def test_render_preview_with_campaign_resolves_branded_c_url(self):
        resp = self.client.post(
            '/api/campaigns/render-preview/',
            data=json.dumps({
                'contact_id': self.contact.id,
                'campaign_id': self.campaign.id,
                'subject': 'Hi',
                'html_content': tracked_email_html(),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('%7Bunique_link%7D', html)
        link = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact, link_type=RECIPIENT)
        self.assertIn(link.short_url, html)

    def test_render_preview_without_campaign_has_no_literal_placeholder(self):
        resp = self.client.post(
            '/api/campaigns/render-preview/',
            data=json.dumps({
                'contact_id': self.contact.id,
                'subject': 'Hi',
                'html_content': tracked_email_html(),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('%7Bunique_link%7D', html)

    def test_preview_tracking_disabled_stays_direct(self):
        self.campaign.track_clicks = False
        self.campaign.save(update_fields=['track_clicks'])
        resp = self.client.post(
            f'/api/campaigns/{self.campaign.id}/preview/',
            data=json.dumps({
                'contact_id': self.contact.id,
                'html_content': tracked_email_html(),
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.json()['html_content']
        self.assertNotIn('{unique_link}', html)
        self.assertNotIn('/c/', html)
        self.assertIn(ODK_URL, html)
        self.assertEqual(
            CampaignTrackingLink.objects.filter(link_type=RECIPIENT).count(), 0)


class ProviderTrackingOptOutTests(TestCase):
    """Problem 1: provider payloads must carry per-message tracking opt-outs."""

    def test_sendgrid_opt_out_payload(self):
        sender = make_sender(Sender.ProviderType.SENDGRID, flag=True)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(202)
            res = SendGridProvider(sender).send_email(
                'a@example.com', 'S', '<p>Hi</p><a href="https://b.example/c/T0KEN123">x</a>')
        self.assertTrue(res.success)
        payload = mocked.call_args.kwargs['json']
        self.assertEqual(
            payload['tracking_settings']['click_tracking'],
            {'enable': False, 'enable_text': False})

    def test_sendgrid_default_payload_unchanged(self):
        sender = make_sender(Sender.ProviderType.SENDGRID, flag=False)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(202)
            SendGridProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertNotIn('tracking_settings', mocked.call_args.kwargs['json'])

    def test_mailgun_opt_out_payload(self):
        sender = make_sender(Sender.ProviderType.MAILGUN, flag=True)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(200, {'id': 'm1'})
            res = MailgunProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertTrue(res.success)
        self.assertEqual(mocked.call_args.kwargs['data']['o:tracking-clicks'], 'no')

    def test_mailgun_default_payload_unchanged(self):
        sender = make_sender(Sender.ProviderType.MAILGUN, flag=False)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(200, {'id': 'm1'})
            MailgunProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertNotIn('o:tracking-clicks', mocked.call_args.kwargs['data'])

    def test_postmark_opt_out_payload(self):
        sender = make_sender(Sender.ProviderType.POSTMARK, flag=True)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(200, {'MessageID': 'p1'})
            res = PostmarkProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertTrue(res.success)
        self.assertEqual(mocked.call_args.kwargs['json']['TrackLinks'], 'None')

    def test_postmark_default_payload_unchanged(self):
        sender = make_sender(Sender.ProviderType.POSTMARK, flag=False)
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(200, {'MessageID': 'p1'})
            PostmarkProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertNotIn('TrackLinks', mocked.call_args.kwargs['json'])

    def test_brevo_payload_carries_branded_html_and_no_tracking_keys(self):
        # Documents the platform limitation: Brevo offers no per-message
        # opt-out, so our payload is unchanged by the flag -- but the HTML
        # we hand to Brevo provably contains our branded /c/ URL.
        branded = '<p>Hi</p><a href="https://marketing.iriscommunications.cloud/c/T0KEN123">x</a></p>'
        for flag in (False, True):
            sender = make_sender(Sender.ProviderType.BREVO, flag=flag)
            with patch('apps.email_providers.providers.requests.post') as mocked:
                mocked.return_value = fake_response(201, {'messageId': 'b1'})
                res = BrevoProvider(sender).send_email('a@example.com', 'S', branded)
            self.assertTrue(res.success)
            payload = mocked.call_args.kwargs['json']
            self.assertEqual(payload['htmlContent'], branded)
            for key in ('tracking_settings', 'o:tracking-clicks', 'TrackLinks',
                        'contactPixelTrackingConsent', 'trackClicks'):
                self.assertNotIn(key, json.dumps(payload))

class ProviderFlagChainTests(TestCase):
    """Part 5: prove the sender flag travels API -> DB -> provider payload."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='chain_admin', email='chain@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_flag_chain_api_to_sendgrid_payload(self):
        resp = self.client.post(
            '/api/senders/',
            data=json.dumps({
                'name': 'Chain Sender',
                'email': 'survey@marketing.iriscommunications.cloud',
                'provider_type': Sender.ProviderType.SENDGRID,
                'disable_provider_click_tracking': True,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        sender = Sender.objects.get(pk=resp.json()['id'])
        self.assertTrue(sender.disable_provider_click_tracking)

        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(202)
            SendGridProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        payload = mocked.call_args.kwargs['json']
        self.assertEqual(
            payload['tracking_settings']['click_tracking'],
            {'enable': False, 'enable_text': False})

        # Flipping the flag off via API removes the opt-out from payloads.
        resp = self.client.patch(
            f'/api/senders/{sender.id}/',
            data=json.dumps({'disable_provider_click_tracking': False}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        sender.refresh_from_db()
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(202)
            SendGridProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertNotIn('tracking_settings', mocked.call_args.kwargs['json'])

    def test_flag_has_no_effect_on_brevo_request(self):
        # Proves Case 4 for Brevo specifically: the flag is stored and read,
        # but Brevo's API offers no tracking opt-out, so the request is
        # byte-identical with the flag on or off.
        payloads = []
        for flag in (False, True):
            sender = make_sender(Sender.ProviderType.BREVO, flag=flag)
            with patch('apps.email_providers.providers.requests.post') as mocked:
                mocked.return_value = fake_response(201, {'messageId': 'b1'})
                BrevoProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
            payloads.append(mocked.call_args.kwargs['json'])
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(
            sorted(payloads[0].keys()), ['htmlContent', 'sender', 'subject', 'to'])

    def test_brevo_send_emits_sanitized_diagnostic(self):
        sender = make_sender(Sender.ProviderType.BREVO, flag=True)
        base = get_shortener_base_url().rstrip('/')
        html = '<p>Hi</p><a href="' + base + '/c/AbC123Xy">Start</a></p>'
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake_response(201, {'messageId': 'b1'})
            with self.assertLogs('apps.email_providers.providers', level='INFO') as logs:
                BrevoProvider(sender).send_email(
                    'qa@example.com', 'S', html,
                    log_context={'campaign_id': 7, 'message_id': 42})
        self.assertTrue(mocked.called)
        line = logs.output[0]
        self.assertIn('sender_id=%d' % sender.id, line)
        self.assertIn('mode=api', line)
        self.assertIn('disable_provider_click_tracking=True', line)
        self.assertIn('campaign_id=7', line)
        self.assertIn('message_id=42', line)
        self.assertIn("'branded_c_urls': 1", line)
        self.assertIn("'unresolved_placeholders': 0", line)
        self.assertIn("'contains_provider_tracking_domain': False", line)
        # Sanitized: masked recipient, no tokens, no secrets, no raw HTML.
        self.assertIn('q***@example.com', line)
        self.assertNotIn('qa@example.com', line)
        self.assertNotIn('AbC123Xy', line)
        self.assertNotIn('test-secret', line)

    def test_sender_flag_round_trip_via_api(self):
        user = User.objects.create_user(
            username='sg_admin', email='sg@example.com', password='password123')
        client = Client()
        client.force_login(user)
        sender = make_sender(Sender.ProviderType.SENDGRID, flag=False)

        resp = client.patch(
            f'/api/senders/{sender.id}/',
            data=json.dumps({'disable_provider_click_tracking': True}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['disable_provider_click_tracking'])
        sender.refresh_from_db()
        self.assertTrue(sender.disable_provider_click_tracking)
