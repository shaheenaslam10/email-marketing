import json
from django.test import TestCase, Client
from django.utils import timezone
from apps.accounts.models import User
from apps.contacts.models import Contact, ContactStatusHistory
from apps.groups.models import ContactGroup
from apps.senders.models import Sender
from apps.campaigns.models import Campaign, CampaignMessage
from apps.campaigns.services import wrap_tracking
from apps.tracking.models import ShortenedLink, RecipientLink, LinkClickEvent, EmailEvent, CampaignTrackingLink
from apps.tracking.utils import generate_secure_token, validate_destination_url, detect_bot_and_device


class UrlShortenerLinkTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin_test', email='admin@example.com', password='password123')
        self.client = Client()
        self.client.force_login(self.user)

        self.sender = Sender.objects.create(
            name='Survey Team',
            email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX
        )

        self.group = ContactGroup.objects.create(name='Field Study 2026')

        self.contact1 = Contact.objects.create(
            first_name='Ali',
            last_name='Khan',
            email='ali@example.com',
            job_id='10021',
            status=Contact.UsageStatus.UNUSED
        )
        self.contact1.groups.add(self.group)

        self.contact2 = Contact.objects.create(
            first_name='Ahmed',
            last_name='Raza',
            email='ahmed@example.com',
            job_id='10022',
            status=Contact.UsageStatus.UNUSED
        )
        self.contact2.groups.add(self.group)

        self.campaign = Campaign.objects.create(
            name='ODK Baseline Survey',
            subject='Your ODK Survey Link',
            sender=self.sender,
            status=Campaign.Status.ACTIVE,
            html_content='<p>Hello, please <a href="https://www.odk.iriscommunications.cloud/project/1/forms/registration" data-link-name="ODK Survey Link">Click Here</a> to start.</p>',
            track_clicks=True,
            track_opens=True
        )
        self.campaign.groups.add(self.group)

    def test_token_generation_and_validation(self):
        """Verify token generator produces random secure tokens between 6 and 12 chars."""
        tokens = {generate_secure_token(7) for _ in range(100)}
        self.assertEqual(len(tokens), 100, "All tokens must be uniquely generated")
        for tok in tokens:
            self.assertEqual(len(tok), 7)
            self.assertTrue(tok.isalnum())

        # URL validation
        self.assertTrue(validate_destination_url('https://www.odk.iriscommunications.cloud/survey/123'))
        self.assertTrue(validate_destination_url('http://example.com/form'))
        self.assertFalse(validate_destination_url('javascript:alert(1)'))
        self.assertFalse(validate_destination_url('data:text/html,hack'))
        self.assertFalse(validate_destination_url('https://evil.com\r\nInjected: header'))
        self.assertFalse(validate_destination_url(''))

    def test_bot_and_scanner_detection(self):
        """Verify detect_bot_and_device detects Microsoft SafeLinks, Google crawlers, and normal browsers."""
        class MockRequest:
            def __init__(self, ua):
                self.META = {'HTTP_USER_AGENT': ua, 'HTTP_REFERER': ''}

        # SafeLinks / Email Security Scanner
        req_scanner = MockRequest("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0 SafeLinks Crawler")
        det_scanner = detect_bot_and_device(req_scanner)
        self.assertEqual(det_scanner['click_type'], 'SUSPECTED_BOT')

        # Web Crawler
        req_bot = MockRequest("Googlebot/2.1 (+http://www.google.com/bot.html)")
        det_bot = detect_bot_and_device(req_bot)
        self.assertEqual(det_bot['click_type'], 'SUSPECTED_BOT')

        # Human Desktop Browser
        req_human = MockRequest("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        det_human = detect_bot_and_device(req_human)
        self.assertEqual(det_human['click_type'], 'HUMAN')
        self.assertEqual(det_human['browser'], 'Chrome')
        self.assertEqual(det_human['device_type'], 'Desktop')

    def test_unique_short_url_per_recipient(self):
        """
        Verify Requirement 2 & 7:
        When sending to Recipient 1 and Recipient 2, the system generates
        different unique short URLs for each recipient pointing to the same destination.
        """
        html = self.campaign.html_content

        wrapped_c1 = wrap_tracking(html, token_str="tok-1", campaign=self.campaign, contact=self.contact1)
        wrapped_c2 = wrap_tracking(html, token_str="tok-2", campaign=self.campaign, contact=self.contact2)

        # Retrieve recipient links (unified /c/ system, type RECIPIENT)
        rl1 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact1,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        rl2 = CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=self.contact2,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT)

        self.assertNotEqual(rl1.tracking_token, rl2.tracking_token, "Tokens must be unique per recipient")
        self.assertNotEqual(rl1.short_url, rl2.short_url, "Short URLs must be unique per recipient")
        self.assertIn('/c/', rl1.short_url)
        self.assertIn('/c/', rl2.short_url)
        self.assertEqual(rl1.shortened_link.original_url, rl2.shortened_link.original_url)

        # Verify href attribute is replaced with unique short URL
        self.assertIn(f'href="{rl1.short_url}"', wrapped_c1)
        self.assertIn(f'href="{rl2.short_url}"', wrapped_c2)

    def test_multiple_urls_in_one_email(self):
        """
        Verify Requirement 7:
        Multiple URLs inside one email are tracked separately.
        """
        multi_html = """
        <p>
            <a href="https://odk.iriscommunications.cloud/survey" data-link-name="Survey">Complete Survey</a>
            <a href="https://iriscommunications.cloud/instructions" data-link-name="Instructions">View Instructions</a>
            <a href="https://marketing.iriscommunications.cloud/home" data-link-name="Website">Visit Website</a>
        </p>
        """
        wrapped = wrap_tracking(multi_html, token_str="tok-multi", campaign=self.campaign, contact=self.contact1)

        rl_links = CampaignTrackingLink.objects.filter(
            campaign=self.campaign, contact=self.contact1,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT)
        self.assertEqual(rl_links.count(), 3, "All three links must have distinct tracking records")

        tokens = [rl.tracking_token for rl in rl_links]
        self.assertEqual(len(set(tokens)), 3, "All 3 tokens must be unique")
        for rl in rl_links:
            self.assertIn(rl.short_url, wrapped)

    def test_short_url_redirect_view(self):
        """
        Verify Requirement 3 & 4:
        GET /:tracking_token performs fast 302 redirect, logs LinkClickEvent with bot/device metadata,
        and increments click counters.
        """
        dest_url = "https://www.odk.iriscommunications.cloud/project/1/forms/registration"
        short_link = ShortenedLink.objects.create(campaign=self.campaign, original_url=dest_url, link_name='ODK Survey')
        recip_link = RecipientLink.objects.create(
            shortened_link=short_link,
            campaign=self.campaign,
            contact=self.contact1,
            tracking_token="A7K29X",
            short_url="https://marketing.iriscommunications.cloud/A7K29X"
        )

        msg = CampaignMessage.objects.create(
            campaign=self.campaign,
            contact=self.contact1,
            to_email=self.contact1.email,
            status=CampaignMessage.Status.SENT
        )

        # 1. Human Click
        resp = self.client.get(
            '/A7K29X/',
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            HTTP_X_FORWARDED_FOR="203.0.113.195"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, dest_url)

        recip_link.refresh_from_db()
        self.assertEqual(recip_link.click_count, 1)
        self.assertEqual(recip_link.human_click_count, 1)
        self.assertEqual(recip_link.bot_click_count, 0)
        self.assertIsNotNone(recip_link.first_clicked_at)

        # Verify LinkClickEvent created
        event = LinkClickEvent.objects.filter(recipient_link=recip_link).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.click_type, LinkClickEvent.ClickType.HUMAN)
        self.assertEqual(event.browser, 'Chrome')
        self.assertEqual(event.device_type, 'Desktop')
        self.assertEqual(event.ip_address, '203.0.113.195')

        # Verify CampaignMessage clicked_at updated
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)
        self.assertIsNotNone(msg.clicked_at)

        # 2. Suspected Bot Click
        resp_bot = self.client.get(
            '/A7K29X/',
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) SafeLinks-Crawler"
        )
        self.assertEqual(resp_bot.status_code, 302)
        recip_link.refresh_from_db()
        self.assertEqual(recip_link.click_count, 2)
        self.assertEqual(recip_link.human_click_count, 1)
        self.assertEqual(recip_link.bot_click_count, 1)

        bot_event = LinkClickEvent.objects.filter(recipient_link=recip_link, click_type=LinkClickEvent.ClickType.SUSPECTED_BOT).first()
        self.assertIsNotNone(bot_event)

        # 3. Invalid Token Returns 404
        resp_invalid = self.client.get('/NONEXIST123/')
        self.assertEqual(resp_invalid.status_code, 404)

    def test_campaign_report_link_recipients_api(self):
        """
        Verify Requirement 6 & 8:
        Recipient-level report API supports filtering (clicked, not_clicked, contact_status),
        pagination, search, and CSV/Excel exports.
        """
        short_link = ShortenedLink.objects.create(campaign=self.campaign, original_url="https://odk.example.com", link_name="ODK Survey")
        RecipientLink.objects.create(
            shortened_link=short_link,
            campaign=self.campaign,
            contact=self.contact1,
            tracking_token="TOK111",
            short_url="https://marketing.iriscommunications.cloud/TOK111",
            click_count=3,
            human_click_count=3
        )
        RecipientLink.objects.create(
            shortened_link=short_link,
            campaign=self.campaign,
            contact=self.contact2,
            tracking_token="TOK222",
            short_url="https://marketing.iriscommunications.cloud/TOK222",
            click_count=0
        )

        # JSON response
        resp = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=all')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['total_count'], 2)

        # Filter Clicked
        resp_clicked = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=clicked')
        self.assertEqual(resp_clicked.json()['total_count'], 1)
        self.assertEqual(resp_clicked.json()['recipients'][0]['email'], 'ali@example.com')

        # Filter Not Clicked
        resp_unclicked = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-recipients/?status=not_clicked')
        self.assertEqual(resp_unclicked.json()['total_count'], 1)
        self.assertEqual(resp_unclicked.json()['recipients'][0]['email'], 'ahmed@example.com')

        # CSV Export
        resp_csv = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-recipients/?export=csv')
        self.assertEqual(resp_csv.status_code, 200)
        self.assertIn('text/csv', resp_csv['Content-Type'])
        self.assertIn('ali@example.com', resp_csv.content.decode('utf-8'))

        # Excel Export
        resp_xlsx = self.client.get(f'/api/campaigns/{self.campaign.id}/report/link-recipients/?export=xlsx')
        self.assertEqual(resp_xlsx.status_code, 200)
        self.assertIn('spreadsheetml', resp_xlsx['Content-Type'])

    def test_contact_activity_timeline_api(self):
        """
        Verify Requirement 11:
        Unified activity timeline returns email sends, link clicks, and status transitions chronologically.
        """
        # Create campaign message
        msg = CampaignMessage.objects.create(
            campaign=self.campaign,
            contact=self.contact1,
            to_email=self.contact1.email,
            status=CampaignMessage.Status.DELIVERED,
            sent_at=timezone.now()
        )

        # Create link click event
        sl = ShortenedLink.objects.create(campaign=self.campaign, original_url="https://odk.example.com", link_name="ODK Survey")
        rl = RecipientLink.objects.create(
            shortened_link=sl,
            campaign=self.campaign,
            contact=self.contact1,
            tracking_token="TL1234",
            short_url="https://marketing.iriscommunications.cloud/TL1234",
            click_count=1
        )
        LinkClickEvent.objects.create(
            recipient_link=rl,
            campaign=self.campaign,
            contact=self.contact1,
            browser="Chrome",
            device_type="Desktop",
            click_type=LinkClickEvent.ClickType.HUMAN
        )

        # Create status change
        ContactStatusHistory.objects.create(
            contact=self.contact1,
            old_status='UNUSED',
            new_status='USED',
            source='ODK_CENTRAL',
            odk_submission_id='sub-9988'
        )

        resp = self.client.get(f'/api/contacts/{self.contact1.id}/timeline/')
        self.assertEqual(resp.status_code, 200)
        events = resp.json()
        self.assertGreaterEqual(len(events), 3)

        types = [e['type'] for e in events]
        self.assertIn('CAMPAIGN_SENT', types)
        self.assertIn('LINK_CLICK', types)
        self.assertIn('STATUS_CHANGE', types)
