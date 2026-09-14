import json
from unittest.mock import MagicMock, patch
from django.test import TestCase, Client
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign
from apps.email_providers.providers import (
    BrevoProvider, SMTPProvider, AmazonSESProvider, MailgunProvider,
)
from apps.tracking.models import (
    ShortenedLink, RecipientLink, LinkClickEvent,
    CampaignTrackingLink, CampaignLinkClickEvent,
)

# Synthetic ODK-shaped URL (same structure/special chars as production URLs,
# but a fake host + token). Never use a real survey token in tests.
ODK_URL = 'https://odk.example.com/f/Ds8TwudfJFvhD0l7Pflj20oH?st=FakeToken123&src=email'

HUMAN_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
BOT_UA = 'Mozilla/5.0 (Windows NT 10.0) SafeLinks-Crawler'
RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT


def make_sender(provider_type, **kwargs):
    sender = Sender.objects.create(
        name='Team',
        email='survey@marketing.iriscommunications.cloud',
        provider_type=provider_type,
        **kwargs
    )
    sender.password_or_key = 'test-secret'
    sender.save(update_fields=['password_or_key_encrypted'])
    return sender


class RecipientClickActivityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='rc_admin', email='rc@example.com', password='password123'
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX,
        )
        self.group = ContactGroup.objects.create(name='RC Group')
        self.contact1 = Contact.objects.create(
            first_name='Ali', last_name='Khan', email='ali@example.com', job_id='9101',
        )
        self.contact2 = Contact.objects.create(
            first_name='Sara', last_name='Ahmed', email='sara@example.com', job_id='9102',
        )
        self.contact1.groups.add(self.group)
        self.contact2.groups.add(self.group)
        self.campaign = Campaign.objects.create(
            name='RC Campaign',
            subject='Hello',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>Hello</p>',
            destination_url=ODK_URL,
        )
        self.campaign.groups.add(self.group)
        self.sl = ShortenedLink.objects.create(
            campaign=self.campaign, original_url=ODK_URL, link_name='SurveyLink_New3')

    def _legacy_click(self, contact, click_type='HUMAN', referrer=''):
        rl = RecipientLink.objects.create(
            shortened_link=self.sl, campaign=self.campaign, contact=contact,
            tracking_token='LEGACY%s' % contact.id,
            short_url='https://marketing.iriscommunications.cloud/LEGACY%s' % contact.id,
        )
        return LinkClickEvent.objects.create(
            recipient_link=rl, campaign=self.campaign, contact=contact,
            browser='Chrome', operating_system='Windows', device_type='Desktop',
            referrer=referrer, click_type=click_type,
        )

    def _new_click(self, contact, click_type='HUMAN', referrer=''):
        link = CampaignTrackingLink.objects.create(
            campaign=self.campaign, link_type=RECIPIENT, contact=contact,
            shortened_link=self.sl, destination_url=ODK_URL,
            name='SurveyLink_New3', tracking_token='CLink%s' % contact.id,
            short_url='https://marketing.iriscommunications.cloud/c/CLink%s' % contact.id,
        )
        return CampaignLinkClickEvent.objects.create(
            link=link, campaign=self.campaign, contact=contact,
            browser='Firefox', operating_system='macOS', device_type='Mobile',
            referrer=referrer, click_type=click_type,
        )

    def test_link_clicks_api_unions_newest_first(self):
        self._legacy_click(self.contact1, referrer='https://mail.example.com/')
        self._new_click(self.contact2)
        # Shareable visit must stay out of the recipient feed.
        shareable = CampaignTrackingLink.objects.create(
            campaign=self.campaign, name='Ad', tracking_token='SH4RE00X',
            short_url='https://marketing.iriscommunications.cloud/c/SH4RE00X')
        CampaignLinkClickEvent.objects.create(link=shareable, campaign=self.campaign)

        resp = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-clicks/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['total_events'], 2)
        self.assertEqual(
            [(l['id'], l['name']) for l in data['links']],
            [(self.sl.id, 'SurveyLink_New3')])
        events = data['events']
        self.assertEqual(len(events), 2)
        # Newest first: the /c/ click was recorded after the legacy one.
        self.assertEqual(events[0]['contact_email'], 'sara@example.com')
        self.assertEqual(events[0]['contact_name'], 'Sara Ahmed')
        self.assertEqual(events[0]['link_name'], 'SurveyLink_New3')
        self.assertIn('/c/', events[0]['short_url'])
        self.assertEqual(events[0]['click_type'], 'Human')
        self.assertEqual(events[0]['browser'], 'Firefox')
        self.assertEqual(events[0]['operating_system'], 'macOS')
        self.assertEqual(events[0]['device_type'], 'Mobile')
        self.assertEqual(events[1]['contact_email'], 'ali@example.com')
        self.assertEqual(events[1]['browser'], 'Chrome')
        self.assertEqual(events[1]['referrer'], 'https://mail.example.com/')

    def test_link_clicks_api_link_filter_and_limit(self):
        sl2 = ShortenedLink.objects.create(
            campaign=self.campaign, original_url='https://example.com/info',
            link_name='Info Page')
        self._legacy_click(self.contact1)
        rl2 = RecipientLink.objects.create(
            shortened_link=sl2, campaign=self.campaign, contact=self.contact2,
            tracking_token='LEGACY99',
            short_url='https://marketing.iriscommunications.cloud/LEGACY99')
        LinkClickEvent.objects.create(
            recipient_link=rl2, campaign=self.campaign, contact=self.contact2)

        filtered = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/link-clicks/?link_id={sl2.id}')
        self.assertEqual(filtered.json()['total_events'], 1)
        self.assertEqual(len(filtered.json()['events']), 1)
        self.assertEqual(filtered.json()['events'][0]['link_name'], 'Info Page')

        limited = self.client.get(
            f'/api/campaigns/{self.campaign.id}/report/link-clicks/?limit=1')
        self.assertEqual(limited.json()['total_events'], 2)
        self.assertEqual(len(limited.json()['events']), 1)

    def test_link_clicks_api_requires_auth_and_excludes_ips(self):
        self._legacy_click(self.contact1)
        resp = self.anon.get(f'/api/campaigns/{self.campaign.id}/report/link-clicks/')
        self.assertIn(resp.status_code, (401, 403))
        authed = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-clicks/')
        self.assertNotIn('ip_address', json.dumps(authed.json()))

    def test_report_page_contains_recipient_activity_card(self):
        page = self.client.get(f'/campaigns/{self.campaign.id}/report/')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('Recipient Click Activity', body)
        self.assertIn('rc-events-tbody', body)
        self.assertIn('loadRecipientClickActivity', body)
        # Shareable section still present below.
        self.assertIn('Shareable Link Activity', body)


class ProviderDiagnosticLoggingTests(TestCase):
    def test_delegated_brevo_smtp_logs_once(self):
        sender = make_sender(Sender.ProviderType.BREVO)
        sender.password_or_key = 'xsmtps-fake-key'
        sender.save(update_fields=['password_or_key_encrypted'])
        with patch('apps.email_providers.providers.smtplib.SMTP'):
            with self.assertLogs('apps.email_providers.providers', level='INFO') as logs:
                res = BrevoProvider(sender).send_email(
                    'a@example.com', 'S', '<p>Hi</p>',
                    log_context={'campaign_id': 3})
        self.assertTrue(res.success)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('via brevo', logs.output[0])
        self.assertIn('mode=smtp-relay', logs.output[0])
        self.assertIn('campaign_id=3', logs.output[0])

    def test_direct_smtp_logs_once(self):
        sender = make_sender(Sender.ProviderType.SMTP, host='smtp.example.com')
        with patch('apps.email_providers.providers.smtplib.SMTP'):
            with self.assertLogs('apps.email_providers.providers', level='INFO') as logs:
                res = SMTPProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertTrue(res.success)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('via smtp', logs.output[0])
        self.assertIn('mode=smtp', logs.output[0])

    def test_ses_without_host_logs_once_as_sandbox(self):
        sender = make_sender(Sender.ProviderType.AMAZON_SES)
        with self.assertLogs('apps.email_providers.providers', level='INFO') as logs:
            res = AmazonSESProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertTrue(res.success)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('via ses', logs.output[0])
        self.assertIn('mode=sandbox', logs.output[0])

    def test_mailgun_logs_api_mode(self):
        sender = make_sender(Sender.ProviderType.MAILGUN)
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {'id': 'm1'}
        with patch('apps.email_providers.providers.requests.post') as mocked:
            mocked.return_value = fake
            with self.assertLogs('apps.email_providers.providers', level='INFO') as logs:
                res = MailgunProvider(sender).send_email('a@example.com', 'S', '<p>Hi</p>')
        self.assertTrue(res.success)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('via mailgun', logs.output[0])
        self.assertIn('mode=api', logs.output[0])

    def test_senders_page_shows_brevo_rewrite_notice(self):
        user = User.objects.create_user(
            username='nt_admin', email='nt@example.com', password='password123')
        client = Client()
        client.force_login(user)
        page = client.get('/settings/senders/')
        self.assertEqual(page.status_code, 200)
        body = page.content.decode('utf-8')
        self.assertIn('snd-brevo-note', body)
        self.assertIn('sendibt3.com', body)
