"""End-to-end recipient lifecycle: assign -> send -> open -> click -> ODK -> USED -> reminder decision.

Pins the authoritative identity chain on real endpoints (no mocks except the
ODK Central sandbox + sandbox mail provider):

    Campaign -> Contact -> CampaignMessage -> CampaignTrackingLink(RECIPIENT)
      -> CampaignLinkClickEvent -> ODK submission (job_id) -> Contact USED
      -> reminder eligible / suppressed
"""
import json

from django.test import TestCase, Client

from apps.accounts.models import User
from apps.campaigns.models import Campaign, CampaignMessage
from apps.campaigns.services import render_content_variables  # noqa: F401 (import guard)
from apps.contacts.models import Contact, ContactStatusHistory
from apps.groups.models import ContactGroup
from apps.odk.models import ODKConnection, ODKProject, ODKForm, ODKFieldMapping, ODKUnmatchedSubmission
from apps.odk.services import run_odk_sync
from apps.reminders.models import ReminderConfiguration
from apps.reminders.services import (
    execute_reminder_cycle, launch_initial_campaign,
    process_single_campaign_message, reminder_eligibility,
)
from apps.sandbox.models import MockODKSubmission, SandboxEmail
from apps.senders.models import Sender
from apps.tracking.models import (
    CampaignTrackingLink, CampaignLinkClickEvent, EmailEvent, TrackingToken,
)

ODK_URL = 'https://odk.example.com/f/FAKE123?st=FakeToken'
HUMAN_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


class RecipientLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='lifecycle_admin', email='lifecycle@example.com', password='password123')
        self.client = Client()
        self.client.force_login(self.user)
        self.anon = Client()

        self.sender = Sender.objects.create(
            name='Survey Team', email='survey@marketing.iriscommunications.cloud',
            provider_type=Sender.ProviderType.SANDBOX, is_active=True)
        self.group = ContactGroup.objects.create(name='Lifecycle Group')

        self.odk_conn = ODKConnection.objects.create(
            name='Mock ODK', base_url='http://localhost:8000/sandbox/api/odk',
            is_mock_sandbox=True, status=ODKConnection.Status.CONNECTED)
        self.odk_project = ODKProject.objects.create(
            connection=self.odk_conn, odk_id=1, name='Pulse 2026')
        self.odk_form = ODKForm.objects.create(
            project=self.odk_project, odk_xml_form_id='pulse_v1', name='Pulse_V1')
        ODKFieldMapping.objects.create(form=self.odk_form, job_id_field='job_id')

        self.campaign = Campaign.objects.create(
            name='Lifecycle Survey', campaign_type=Campaign.Type.SURVEY_REMINDER,
            status=Campaign.Status.ACTIVE, sender=self.sender,
            odk_form=self.odk_form, subject='Survey for {{job_id}}',
            html_content='<p>Dear {{first_name}},</p><p>{{survey_tracking_url}}</p>',
            destination_url=ODK_URL)
        self.campaign.groups.add(self.group)
        ReminderConfiguration.objects.create(
            campaign=self.campaign, enabled=True, interval_value=2,
            interval_unit=ReminderConfiguration.Unit.DAYS,
            max_reminders=5, sync_odk_before_send=True, stop_when_used=True)

    # -- helpers ---------------------------------------------------------
    def _make_contact(self, name, email, phone, job_id):
        c = Contact.objects.create(
            name=name, first_name=name.split(' ')[0], email=email,
            phone_number=phone, job_id=job_id,
            status=Contact.UsageStatus.UNUSED)
        self.group.contacts.add(c)
        return c

    def _initial_msg(self, contact):
        return CampaignMessage.objects.get(
            campaign=self.campaign, contact=contact,
            message_type=CampaignMessage.MessageType.INITIAL)

    def _recipient_link(self, contact):
        return CampaignTrackingLink.objects.get(
            campaign=self.campaign, contact=contact,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT)

    def _open_email(self, contact):
        msg = self._initial_msg(contact)
        token = TrackingToken.objects.get(message=msg).token
        resp = self.anon.get('/t/open/%s/' % token)
        self.assertEqual(resp.status_code, 200)
        return resp

    def _click_link(self, contact):
        link = self._recipient_link(contact)
        return self.anon.get('/c/%s/' % link.tracking_token, HTTP_USER_AGENT=HUMAN_UA), link

    def _submit_odk(self, job_id, submission_id, email=''):
        MockODKSubmission.objects.create(
            submission_id=submission_id, project_id='1', form_id='pulse_v1',
            job_id=job_id, respondent_email=email)
        return run_odk_sync(self.odk_form)

    # -- the Ali story: completed -> suppressed --------------------------
    def test_01_full_lifecycle_completed_recipient_suppressed(self):
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')

        # 1-2. assign + send
        self.assertEqual(launch_initial_campaign(self.campaign), 1)
        msg = self._initial_msg(ali)
        self.assertEqual(msg.status, CampaignMessage.Status.DELIVERED)
        self.assertIsNotNone(msg.sent_at)

        # 3-4. token belongs to Ali; exactly one recipient link (no dupes)
        link = self._recipient_link(ali)
        self.assertEqual(link.contact_id, ali.id)
        self.assertEqual(len(link.tracking_token), 8)

        # 5-6. delivered mail carries Ali's first-party /c/ URL
        email = SandboxEmail.objects.filter(to_email='ali@example.com').latest('created_at')
        self.assertIn('/c/%s' % link.tracking_token, email.html_content)
        self.assertNotIn('sendibt3', email.html_content)
        self.assertNotIn('/t/click/', email.html_content)
        self.assertNotIn('{unique_link}', email.html_content)
        self.assertNotIn('survey_tracking_url', email.html_content)

        # 7. open (distinct from click)
        self._open_email(ali)
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.OPENED)
        self.assertIsNotNone(msg.opened_at)
        self.assertIsNone(msg.clicked_at)
        self.assertTrue(EmailEvent.objects.filter(
            message=msg, event_type=EmailEvent.EventType.OPEN).exists())

        # 8-10. click: 302 to ODK, event attributed to Ali
        click, _ = self._click_link(ali)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        event = CampaignLinkClickEvent.objects.get(link=link)
        self.assertEqual(event.contact_id, ali.id)
        self.assertEqual(event.campaign_id, self.campaign.id)
        msg.refresh_from_db()
        self.assertEqual(msg.status, CampaignMessage.Status.CLICKED)

        # click != completion: still UNUSED + reminder eligible
        ali.refresh_from_db()
        self.assertEqual(ali.status, Contact.UsageStatus.UNUSED)
        self.assertEqual(
            reminder_eligibility(ali, self.campaign),
            {'eligible': True, 'reason': 'Survey not completed'})

        # 11-12. ODK submission matched by job_id -> USED
        job = self._submit_odk('JOB-9001', 'sub-ali-1', email='ali@example.com')
        self.assertEqual(job.contacts_marked_used, 1)
        ali.refresh_from_db()
        self.assertEqual(ali.status, Contact.UsageStatus.USED)
        self.assertEqual(ali.odk_submission_id, 'sub-ali-1')
        self.assertIsNotNone(ali.odk_submitted_at)
        self.assertEqual(ali.status_source, 'ODK_CENTRAL')

        # 13-14. report shows clicked + completed + suppressed
        resp = self.client.get(
            '/api/campaigns/%d/report/link-recipients/' % self.campaign.id)
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.json()['recipients'] if r['contact_id'] == ali.id)
        self.assertEqual(row['name'], 'Ali Abbas')
        self.assertEqual(row['phone'], '03001234567')
        self.assertEqual(row['tracking_token'], link.tracking_token)
        self.assertEqual(row['link_status'], 'Clicked')
        self.assertEqual(row['contact_status'], 'USED')
        self.assertNotEqual(row['email_sent_at'], '-')
        self.assertNotEqual(row['email_opened_at'], '-')
        self.assertNotEqual(row['completed_at'], '-')
        self.assertFalse(row['reminder_eligible'])
        self.assertEqual(row['reminder_reason'], 'Survey completed')

        # per-click activity keeps name + phone + email + token on the event
        resp = self.client.get(
            '/api/campaigns/%d/report/link-clicks/' % self.campaign.id)
        evt = resp.json()['events'][0]
        self.assertEqual(evt['contact_name'], 'Ali Abbas')
        self.assertEqual(evt['contact_phone'], '03001234567')
        self.assertEqual(evt['contact_email'], 'ali@example.com')
        self.assertEqual(evt['tracking_token'], link.tracking_token)

        # 15. reminder cycle sends Ali nothing
        cycle = execute_reminder_cycle(self.campaign)
        self.assertEqual(cycle.eligible_count, 0)
        self.assertFalse(CampaignMessage.objects.filter(
            campaign=self.campaign, contact=ali,
            message_type=CampaignMessage.MessageType.REMINDER).exists())

        # timeline is complete and 200
        resp = self.client.get('/api/contacts/%d/timeline/' % ali.id)
        self.assertEqual(resp.status_code, 200)
        types = {e['type'] for e in resp.json()}
        self.assertTrue({'CAMPAIGN_SENT', 'EMAIL_OPENED', 'LINK_CLICK', 'STATUS_CHANGE'} <= types)

    # -- the Sara story: incomplete -> reminded --------------------------
    def test_02_incomplete_recipient_reminded_completed_is_not(self):
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        launch_initial_campaign(self.campaign)
        self._open_email(ali)
        self._open_email(sara)
        self._click_link(ali)
        self._click_link(sara)
        self._submit_odk('JOB-9001', 'sub-ali-2')

        cycle = execute_reminder_cycle(self.campaign)
        self.assertEqual(cycle.eligible_count, 1)

        sara_rem = CampaignMessage.objects.get(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER)
        self.assertEqual(sara_rem.status, CampaignMessage.Status.DELIVERED)
        self.assertIn('[Reminder]', sara_rem.subject)
        # reminder reuses Sara's recipient link (no duplicate token)
        self.assertEqual(CampaignTrackingLink.objects.filter(
            campaign=self.campaign, contact=sara,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT).count(), 1)
        self.assertEqual(
            SandboxEmail.objects.filter(to_email='sara@example.com').count(), 2)
        self.assertFalse(CampaignMessage.objects.filter(
            campaign=self.campaign, contact=ali,
            message_type=CampaignMessage.MessageType.REMINDER).exists())

        resp = self.client.get(
            '/api/campaigns/%d/report/link-recipients/' % self.campaign.id)
        rows = {r['contact_id']: r for r in resp.json()['recipients']}
        self.assertTrue(rows[sara.id]['reminder_eligible'])
        self.assertEqual(rows[sara.id]['reminder_reason'], 'Survey not completed')
        self.assertFalse(rows[ali.id]['reminder_eligible'])

    # -- A/B/C: opened/clicked matrix ------------------------------------
    def test_03_open_click_states_stay_distinct(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        bob = self._make_contact('Bob Raza', 'bob@example.com', '03211112222', 'JOB-9003')
        ahmed = self._make_contact('Ahmed Raza', 'ahmed@example.com', '03213334444', 'JOB-9004')
        launch_initial_campaign(self.campaign)

        self._open_email(sara)
        self._click_link(sara)   # opened + clicked, not completed
        self._open_email(bob)    # opened only

        sara_msg = self._initial_msg(sara)
        bob_msg = self._initial_msg(bob)
        ahmed_msg = self._initial_msg(ahmed)
        self.assertIsNotNone(sara_msg.opened_at)
        self.assertIsNotNone(sara_msg.clicked_at)
        self.assertIsNotNone(bob_msg.opened_at)
        self.assertIsNone(bob_msg.clicked_at)  # open is not a click
        self.assertEqual(bob_msg.status, CampaignMessage.Status.OPENED)
        self.assertIsNone(ahmed_msg.opened_at)  # sent only
        self.assertIsNone(ahmed_msg.clicked_at)
        self.assertFalse(CampaignLinkClickEvent.objects.filter(contact=bob).exists())
        for c in (sara, bob, ahmed):
            c.refresh_from_db()
            self.assertEqual(c.status, Contact.UsageStatus.UNUSED)
            self.assertTrue(reminder_eligibility(c, self.campaign)['eligible'])

    # -- D: completed without a click ------------------------------------
    def test_04_completed_without_click_is_suppressed(self):
        ahmed = self._make_contact('Ahmed Raza', 'ahmed@example.com', '03213334444', 'JOB-9004')
        launch_initial_campaign(self.campaign)
        self._submit_odk('JOB-9004', 'sub-ahmed-1')
        ahmed.refresh_from_db()
        self.assertEqual(ahmed.status, Contact.UsageStatus.USED)
        self.assertFalse(CampaignLinkClickEvent.objects.filter(contact=ahmed).exists())
        cycle = execute_reminder_cycle(self.campaign)
        self.assertEqual(cycle.eligible_count, 0)

    # -- E: duplicate ODK sync -------------------------------------------
    def test_05_duplicate_odk_sync_is_idempotent(self):
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')
        self._submit_odk('JOB-9001', 'sub-ali-3')
        job2 = run_odk_sync(self.odk_form)
        self.assertEqual(job2.submissions_checked, 0)
        self.assertEqual(job2.contacts_marked_used, 0)
        ali.refresh_from_db()
        self.assertEqual(ali.status, Contact.UsageStatus.USED)
        self.assertEqual(ali.odk_submission_id, 'sub-ali-3')
        self.assertEqual(ContactStatusHistory.objects.filter(
            contact=ali, new_status=Contact.UsageStatus.USED).count(), 1)

    # -- F: duplicate execution safety -----------------------------------
    def test_06_repeated_runs_create_no_duplicates(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        launch_initial_campaign(self.campaign)
        # re-launch dispatches nothing new
        self.assertEqual(launch_initial_campaign(self.campaign), 0)
        self.assertEqual(CampaignMessage.objects.filter(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.INITIAL).count(), 1)
        # pre-queued reminder row is reused, not duplicated
        CampaignMessage.objects.create(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1, to_email=sara.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        execute_reminder_cycle(self.campaign)
        rems = CampaignMessage.objects.filter(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER)
        self.assertEqual(rems.count(), 1)
        self.assertEqual(rems[0].status, CampaignMessage.Status.DELIVERED)
        # a second click is a new event on the SAME link (no new token)
        self._click_link(sara)
        self._click_link(sara)
        self.assertEqual(CampaignTrackingLink.objects.filter(
            campaign=self.campaign, contact=sara).count(), 1)
        self.assertEqual(CampaignLinkClickEvent.objects.filter(contact=sara).count(), 2)

    # -- G: paused --------------------------------------------------------
    def test_07_paused_campaign_links_work_reminders_stop(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        launch_initial_campaign(self.campaign)
        self.campaign.status = Campaign.Status.PAUSED
        self.campaign.save(update_fields=['status'])

        click, link = self._click_link(sara)
        self.assertEqual(click.status_code, 302)
        self.assertEqual(click.url, ODK_URL)
        self.assertTrue(CampaignLinkClickEvent.objects.filter(link=link).exists())

        with self.assertRaises(ValueError):
            execute_reminder_cycle(self.campaign)
        queued = CampaignMessage.objects.create(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1, to_email=sara.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        self.assertFalse(process_single_campaign_message(queued.id))
        queued.refresh_from_db()
        self.assertEqual(queued.status, CampaignMessage.Status.SKIPPED)
        self.assertEqual(queued.skip_reason, CampaignMessage.SkipReason.CAMPAIGN_PAUSED)

        # resume -> sending works again
        self.campaign.status = Campaign.Status.ACTIVE
        self.campaign.save(update_fields=['status'])
        queued2 = CampaignMessage.objects.create(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=2, to_email=sara.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        self.assertTrue(process_single_campaign_message(queued2.id))

    # -- H: cancelled -----------------------------------------------------
    def test_08_cancelled_campaign_blocks_links_and_reminders(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        launch_initial_campaign(self.campaign)
        self.campaign.status = Campaign.Status.CANCELLED
        self.campaign.save(update_fields=['status'])

        click, link = self._click_link(sara)
        self.assertEqual(click.status_code, 410)
        self.assertFalse(CampaignLinkClickEvent.objects.filter(link=link).exists())
        with self.assertRaises(ValueError):
            execute_reminder_cycle(self.campaign)

    # -- I/J: invalid token + inactive link -------------------------------
    def test_09_invalid_token_returns_404(self):
        resp = self.anon.get('/c/NOPE1234/', HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(CampaignLinkClickEvent.objects.count(), 0)

    def test_10_inactive_link_returns_410_and_tracks_nothing(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        launch_initial_campaign(self.campaign)
        link = self._recipient_link(sara)
        link.is_active = False
        link.save(update_fields=['is_active'])
        resp = self.anon.get('/c/%s/' % link.tracking_token, HTTP_USER_AGENT=HUMAN_UA)
        self.assertEqual(resp.status_code, 410)
        self.assertFalse(CampaignLinkClickEvent.objects.filter(link=link).exists())

    # -- ODK matching safety ----------------------------------------------
    def test_11_odk_submission_matches_only_the_right_recipient(self):
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')

        # unknown + empty job_ids: unmatched, nobody completed
        self._submit_odk('JOB-NOBODY', 'sub-ghost-1')
        self._submit_odk('', 'sub-empty-1')
        self.assertEqual(ODKUnmatchedSubmission.objects.count(), 2)
        for c in (ali, sara):
            c.refresh_from_db()
            self.assertEqual(c.status, Contact.UsageStatus.UNUSED)

        # email fallback matches Sara only
        job = self._submit_odk('', 'sub-sara-email-1', email='sara@example.com')
        self.assertEqual(job.contacts_marked_used, 1)
        ali.refresh_from_db()
        sara.refresh_from_db()
        self.assertEqual(ali.status, Contact.UsageStatus.UNUSED)
        self.assertEqual(sara.status, Contact.UsageStatus.USED)
        self.assertEqual(sara.odk_submission_id, 'sub-sara-email-1')

        # job_id match completes Ali
        self._submit_odk('JOB-9001', 'sub-ali-4')
        ali.refresh_from_db()
        self.assertEqual(ali.status, Contact.UsageStatus.USED)

    # -- send-time recheck -------------------------------------------------
    def test_12_reminder_rechecks_completion_at_send_time(self):
        sara = self._make_contact('Sara Khan', 'sara@example.com', '03129876543', 'JOB-9002')
        queued = CampaignMessage.objects.create(
            campaign=self.campaign, contact=sara,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1, to_email=sara.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        # ODK completion lands AFTER queueing, BEFORE dispatch
        self._submit_odk('JOB-9002', 'sub-sara-1')
        self.assertFalse(process_single_campaign_message(queued.id))
        queued.refresh_from_db()
        self.assertEqual(queued.status, CampaignMessage.Status.SKIPPED)
        self.assertEqual(queued.skip_reason, CampaignMessage.SkipReason.CONTACT_USED)
        self.assertTrue(EmailEvent.objects.filter(
            message=queued, event_type=EmailEvent.EventType.SKIPPED).exists())
        self.assertEqual(
            SandboxEmail.objects.filter(to_email='sara@example.com').count(), 0)

    # -- timeline ----------------------------------------------------------
    def test_13_timeline_covers_lifecycle_without_errors(self):
        # unsent message: all timestamps None (regression: no created_at crash)
        queued_contact = self._make_contact(
            'Queued Quinn', 'queued@example.com', '03000000000', 'JOB-9010')
        CampaignMessage.objects.create(
            campaign=self.campaign, contact=queued_contact,
            message_type=CampaignMessage.MessageType.INITIAL,
            reminder_sequence=0, to_email=queued_contact.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        resp = self.client.get('/api/contacts/%d/timeline/' % queued_contact.id)
        self.assertEqual(resp.status_code, 200)

        # failed message: error recorded, no timestamps (must not crash either)
        failed_contact = self._make_contact(
            'Failed Fern', 'failed@example.com', '03000000001', 'JOB-9011')
        CampaignMessage.objects.create(
            campaign=self.campaign, contact=failed_contact,
            message_type=CampaignMessage.MessageType.INITIAL,
            reminder_sequence=0, to_email=failed_contact.email, subject='q',
            status=CampaignMessage.Status.FAILED, error_message='SMTP refused')
        resp = self.client.get('/api/contacts/%d/timeline/' % failed_contact.id)
        self.assertEqual(resp.status_code, 200)

        # full lifecycle + suppressed reminder
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')
        launch_initial_campaign(self.campaign)
        self._open_email(ali)
        self._click_link(ali)
        self._submit_odk('JOB-9001', 'sub-ali-5')
        skipped = CampaignMessage.objects.create(
            campaign=self.campaign, contact=ali,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=1, to_email=ali.email, subject='q',
            status=CampaignMessage.Status.QUEUED)
        process_single_campaign_message(skipped.id)
        resp = self.client.get('/api/contacts/%d/timeline/' % ali.id)
        self.assertEqual(resp.status_code, 200)
        events = resp.json()
        types = {e['type'] for e in events}
        self.assertTrue(
            {'CAMPAIGN_SENT', 'EMAIL_OPENED', 'LINK_CLICK', 'STATUS_CHANGE',
             'REMINDER_SUPPRESSED'} <= types)
        suppressed = next(e for e in events if e['type'] == 'REMINDER_SUPPRESSED')
        self.assertIn('CONTACT_USED', suppressed['description'])
        for e in events:
            self.assertIn('timestamp', e)

    # -- eligibility matrix -------------------------------------------------
    def test_14_reminder_eligibility_reasons(self):
        base = self._make_contact('Base Case', 'base@example.com', '03001112222', 'JOB-9020')
        self.assertEqual(reminder_eligibility(base, self.campaign),
                         {'eligible': True, 'reason': 'Survey not completed'})
        self.assertTrue(reminder_eligibility(base)['eligible'])

        used = self._make_contact('Used U', 'used@example.com', '03001113333', 'JOB-9021')
        used.status = Contact.UsageStatus.USED
        used.save(update_fields=['status'])
        self.assertEqual(reminder_eligibility(used, self.campaign),
                         {'eligible': False, 'reason': 'Survey completed'})

        unsub = self._make_contact('Unsub U', 'unsub@example.com', '03001114444', 'JOB-9022')
        unsub.unsubscribed = True
        unsub.save(update_fields=['unsubscribed'])
        self.assertEqual(reminder_eligibility(unsub, self.campaign)['reason'], 'Unsubscribed')

        bounced = self._make_contact('Bounced B', 'bounced@example.com', '03001115555', 'JOB-9023')
        bounced.email_status = Contact.EmailStatus.HARD_BOUNCE
        bounced.save(update_fields=['email_status'])
        self.assertIn('hard bounce',
                      reminder_eligibility(bounced, self.campaign)['reason'].lower())

        self.campaign.status = Campaign.Status.PAUSED
        self.campaign.save(update_fields=['status'])
        self.assertEqual(reminder_eligibility(base, self.campaign)['reason'], 'Campaign paused')
        self.campaign.status = Campaign.Status.CANCELLED
        self.campaign.save(update_fields=['status'])
        self.assertEqual(reminder_eligibility(base, self.campaign)['reason'], 'Campaign cancelled')

        self.campaign.status = Campaign.Status.ACTIVE
        self.campaign.save(update_fields=['status'])
        cfg = self.campaign.reminder_config
        cfg.enabled = False
        cfg.save(update_fields=['enabled'])
        self.assertEqual(reminder_eligibility(base, self.campaign)['reason'], 'Reminders disabled')

    # -- report API shape ----------------------------------------------------
    def test_15_lifecycle_report_columns_and_csv(self):
        ali = self._make_contact('Ali Abbas', 'ali@example.com', '03001234567', 'JOB-9001')
        launch_initial_campaign(self.campaign)
        self._open_email(ali)
        self._click_link(ali)
        self._submit_odk('JOB-9001', 'sub-ali-6')

        resp = self.client.get(
            '/api/campaigns/%d/report/link-recipients/' % self.campaign.id)
        row = next(r for r in resp.json()['recipients'] if r['contact_id'] == ali.id)
        for key in ('phone', 'email_sent_at', 'email_opened_at', 'tracking_token',
                    'completed_at', 'odk_submission_id', 'reminder_eligible',
                    'reminder_reason'):
            self.assertIn(key, row)
        self.assertEqual(row['odk_submission_id'], 'sub-ali-6')

        csv_resp = self.client.get(
            '/api/campaigns/%d/report/link-recipients/?export=csv' % self.campaign.id)
        self.assertEqual(csv_resp.status_code, 200)
        body = csv_resp.content.decode('utf-8')
        self.assertIn('Reminder Eligible', body)
        self.assertIn('Survey completed', body)
        self.assertIn('03001234567', body)

        xlsx_resp = self.client.get(
            '/api/campaigns/%d/report/link-recipients/?export=xlsx' % self.campaign.id)
        self.assertEqual(xlsx_resp.status_code, 200)
        self.assertIn('spreadsheetml', xlsx_resp['Content-Type'])
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(xlsx_resp.content)) as zf:
            blob = b' '.join(
                zf.read(n) for n in zf.namelist() if n.endswith('.xml'))
        for needle in (b'Reminder Eligible', b'Reminder Reason', b'Phone',
                       b'Email Sent', b'Email Opened', b'Tracking Token',
                       b'Completed At', b'Survey completed', b'03001234567'):
            self.assertIn(needle, blob)
