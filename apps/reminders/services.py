import logging
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from apps.campaigns.models import Campaign, CampaignMessage
from apps.contacts.models import Contact
from apps.reminders.models import ReminderConfiguration, ReminderCycle
from apps.tracking.models import EmailEvent, TrackingToken
from apps.email_providers.providers import get_email_provider
from apps.campaigns.services import render_content_variables, wrap_tracking
from apps.odk.services import run_odk_sync, sync_dataset_entities

logger = logging.getLogger(__name__)


def process_single_campaign_message(message_id: int) -> bool:
    """
    Worker function to dispatch a single campaign or reminder message.
    Enforces the CRITICAL send-time safety check (Section 44).
    """
    try:
        message = CampaignMessage.objects.select_related('campaign', 'contact', 'campaign__sender').get(id=message_id)
    except CampaignMessage.DoesNotExist:
        return False

    if message.status not in [CampaignMessage.Status.QUEUED, CampaignMessage.Status.FAILED]:
        return False

    contact = message.contact
    campaign = message.campaign
    sender = campaign.sender

    # CRITICAL SEND-TIME SAFETY CHECK (Section 44 & 98)
    contact.refresh_from_db()

    if message.message_type == CampaignMessage.MessageType.REMINDER:
        if contact.status != Contact.UsageStatus.UNUSED:
            message.status = CampaignMessage.Status.SKIPPED
            message.skip_reason = CampaignMessage.SkipReason.CONTACT_USED
            message.save(update_fields=['status', 'skip_reason'])
            EmailEvent.objects.create(
                message=message,
                event_type=EmailEvent.EventType.SKIPPED,
                metadata={'reason': 'Contact became Used before reminder send'}
            )
            logger.info("Skipped reminder for contact %s: status is %s", contact.email, contact.status)
            return False

    if contact.unsubscribed:
        message.status = CampaignMessage.Status.SKIPPED
        message.skip_reason = CampaignMessage.SkipReason.UNSUBSCRIBED
        message.save(update_fields=['status', 'skip_reason'])
        return False

    if contact.email_status != Contact.EmailStatus.ACTIVE:
        message.status = CampaignMessage.Status.SKIPPED
        message.skip_reason = CampaignMessage.SkipReason.HARD_BOUNCE if contact.email_status == Contact.EmailStatus.HARD_BOUNCE else CampaignMessage.SkipReason.BLOCKED
        message.save(update_fields=['status', 'skip_reason'])
        return False

    if campaign.status == Campaign.Status.PAUSED:
        message.status = CampaignMessage.Status.SKIPPED
        message.skip_reason = CampaignMessage.SkipReason.CAMPAIGN_PAUSED
        message.save(update_fields=['status', 'skip_reason'])
        return False

    # Check sender rate limits (Section 52)
    allowed, limit_reason = sender.check_rate_limits()
    if not allowed:
        message.error_message = f"Rate limit reached: {limit_reason}"
        message.retry_count += 1
        message.save(update_fields=['error_message', 'retry_count'])
        return False

    # Create tracking token
    token, _ = TrackingToken.objects.get_or_create(message=message)

    # Determine template content (custom reminder or main campaign content)
    subject_tmpl = (message.subject or campaign.subject or "").strip()
    html_tmpl = campaign.html_content
    text_tmpl = campaign.text_content

    if message.message_type == CampaignMessage.MessageType.REMINDER:
        if hasattr(campaign, 'reminder_config') and campaign.reminder_config:
            rem_cfg = campaign.reminder_config
            if rem_cfg.custom_subject and rem_cfg.custom_subject.strip():
                subject_tmpl = rem_cfg.custom_subject.strip()
            elif rem_cfg.reminder_template and rem_cfg.reminder_template.subject:
                subject_tmpl = rem_cfg.reminder_template.subject.strip()

            if rem_cfg.custom_html_content:
                html_tmpl = rem_cfg.custom_html_content
            elif rem_cfg.reminder_template:
                html_tmpl = rem_cfg.reminder_template.html_content
                text_tmpl = rem_cfg.reminder_template.text_content

        # Ensure [Reminder] is in the subject for reminder messages
        if not ('[reminder]' in subject_tmpl.lower() or 'reminder:' in subject_tmpl.lower()):
            subject_tmpl = f"{subject_tmpl} [Reminder]"

    # Render personalization variables (Section 14 & 35)
    rendered_subject = render_content_variables(subject_tmpl, contact)
    rendered_html = render_content_variables(html_tmpl, contact)
    rendered_text = render_content_variables(text_tmpl, contact) if text_tmpl else ""

    # Wrap tracking pixel and click redirects (Section 75 & 76, Requirement 2 & 10)
    final_html = wrap_tracking(
        html_content=rendered_html,
        token_str=str(token.token),
        track_opens=campaign.track_opens,
        track_clicks=campaign.track_clicks,
        campaign=campaign,
        contact=contact
    )

    message.subject = rendered_subject
    message.save(update_fields=['subject'])

    # Send through configured provider
    provider = get_email_provider(sender)
    res = provider.send_email(
        to_email=contact.email,
        subject=rendered_subject,
        html_content=final_html,
        text_content=rendered_text,
        reply_to=sender.reply_to
    )

    now = timezone.now()

    if res.success:
        message.status = CampaignMessage.Status.DELIVERED
        message.sent_at = now
        message.delivered_at = now
        message.provider_message_id = res.provider_message_id or ""
        message.save(update_fields=['status', 'sent_at', 'delivered_at', 'provider_message_id'])

        sender.record_send()

        # Update contact metrics
        contact.last_email_sent_at = now
        if message.message_type == CampaignMessage.MessageType.REMINDER:
            contact.last_reminder_sent_at = now
            contact.reminder_count += 1
            contact.save(update_fields=['last_email_sent_at', 'last_reminder_sent_at', 'reminder_count'])
        else:
            contact.save(update_fields=['last_email_sent_at'])

        EmailEvent.objects.create(message=message, event_type=EmailEvent.EventType.SENT)
        EmailEvent.objects.create(message=message, event_type=EmailEvent.EventType.DELIVERED)
        return True
    else:
        message.status = CampaignMessage.Status.FAILED
        message.error_message = res.error_message or "Send failed"
        message.retry_count += 1
        message.save(update_fields=['status', 'error_message', 'retry_count'])
        return False


def execute_reminder_cycle(campaign: Campaign, manual_trigger: bool = False, custom_subject: str = None, batch_size: int = None) -> ReminderCycle:
    """
    Executes a reminder cycle strictly adhering to Section 42, 43, 44, 45, 98:
    1. Synchronize ODK Central if configured.
    2. Mark matching JobIDs as USED.
    3. Retrieve strictly UNUSED eligible contacts.
    4. Queue reminder messages with unique constraint.
    5. Process message queue with send-time status check (supporting batching).
    """
    if not hasattr(campaign, 'reminder_config'):
        if manual_trigger:
            rem_cfg = ReminderConfiguration.objects.create(campaign=campaign, enabled=True)
        else:
            raise ValueError("Campaign does not have reminder configuration")
    else:
        rem_cfg = campaign.reminder_config

    if not rem_cfg.enabled and not manual_trigger:
        raise ValueError("Automated reminders are not enabled for this campaign")

    if campaign.status not in [Campaign.Status.ACTIVE, Campaign.Status.SCHEDULED]:
        if manual_trigger:
            campaign.status = Campaign.Status.ACTIVE
            campaign.save(update_fields=['status'])
        else:
            raise ValueError(f"Cannot run reminders for campaign in status {campaign.status}")

    # Determine sequence number
    last_cycle = rem_cfg.cycles.order_by('-cycle_number').first()
    cycle_num = (last_cycle.cycle_number + 1) if last_cycle else 1

    if cycle_num > rem_cfg.max_reminders:
        if manual_trigger:
            rem_cfg.max_reminders = cycle_num
            rem_cfg.save(update_fields=['max_reminders'])
        else:
            campaign.status = Campaign.Status.COMPLETED
            campaign.save(update_fields=['status'])
            raise ValueError(f"Maximum reminders ({rem_cfg.max_reminders}) reached.")

    cycle = ReminderCycle.objects.create(
        reminder_config=rem_cfg,
        cycle_number=cycle_num,
        scheduled_for=timezone.now(),
        status=ReminderCycle.Status.SYNCING_ODK
    )

    # 1. Mandatory Pre-Reminder ODK Sync (Section 42 & 45 & 98)
    if rem_cfg.sync_odk_before_send or manual_trigger:
        if campaign.odk_dataset:
            try:
                sync_dataset_entities(campaign.odk_dataset)
            except Exception as e:
                logger.warning("Pre-reminder ODK dataset sync warning on campaign %s: %s", campaign.id, e)
        elif campaign.odk_form:
            try:
                run_odk_sync(campaign.odk_form, trigger_source='PRE_REMINDER')
            except Exception as e:
                logger.warning("Pre-reminder ODK sync warning on campaign %s: %s", campaign.id, e)

    # 2. Retrieve only UNUSED contacts belonging to campaign groups (Section 6, 42, 43)
    cycle.status = ReminderCycle.Status.QUEUED
    cycle.save(update_fields=['status'])

    campaign_groups = campaign.groups.all()
    eligible_contacts = list(Contact.objects.filter(
        groups__in=campaign_groups,
        status=Contact.UsageStatus.UNUSED,
        unsubscribed=False,
        email_status=Contact.EmailStatus.ACTIVE
    ).distinct())

    cycle.eligible_count = len(eligible_contacts)
    cycle.still_unused_count = cycle.eligible_count
    cycle.save(update_fields=['eligible_count', 'still_unused_count'])

    sent_count = 0
    delivered_count = 0

    # Determine reminder subject
    if custom_subject and custom_subject.strip():
        reminder_subject = custom_subject.strip()
    elif rem_cfg.custom_subject and rem_cfg.custom_subject.strip():
        reminder_subject = rem_cfg.custom_subject.strip()
    elif rem_cfg.reminder_template and rem_cfg.reminder_template.subject:
        reminder_subject = rem_cfg.reminder_template.subject.strip()
    else:
        reminder_subject = (campaign.subject or campaign.name or 'Survey').strip()

    if not ('[reminder]' in reminder_subject.lower() or 'reminder:' in reminder_subject.lower()):
        reminder_subject = f"{reminder_subject} [Reminder]"

    # Queue messages with Section 54 duplicate protection and batch sending support
    b_limit = batch_size if (batch_size and batch_size > 0) else len(eligible_contacts)

    for idx, contact in enumerate(eligible_contacts):
        msg, created = CampaignMessage.objects.get_or_create(
            campaign=campaign,
            contact=contact,
            message_type=CampaignMessage.MessageType.REMINDER,
            reminder_sequence=cycle_num,
            defaults={
                'to_email': contact.email,
                'subject': reminder_subject,
                'status': CampaignMessage.Status.QUEUED
            }
        )
        if not created and msg.status in [CampaignMessage.Status.QUEUED, CampaignMessage.Status.FAILED]:
            msg.subject = reminder_subject
            msg.save(update_fields=['subject'])

        if created:
            # Dispatch message immediately if within batch limit
            if idx < b_limit:
                success = process_single_campaign_message(msg.id)
                if success:
                    sent_count += 1
                    delivered_count += 1

    now = timezone.now()
    cycle.sent_count = sent_count
    cycle.delivered_count = delivered_count
    cycle.executed_at = now
    cycle.status = ReminderCycle.Status.COMPLETED
    cycle.save()

    # Calculate next reminder schedule
    if rem_cfg.interval_unit == ReminderConfiguration.Unit.DAYS:
        delta = timedelta(days=rem_cfg.interval_value)
    else:
        delta = timedelta(weeks=rem_cfg.interval_value)

    rem_cfg.reminders_sent_count += 1
    rem_cfg.last_run_at = now
    rem_cfg.next_run_at = now + delta
    rem_cfg.save(update_fields=['reminders_sent_count', 'last_run_at', 'next_run_at'])

    return cycle


def launch_initial_campaign(campaign: Campaign, batch_size: int = None, force_resend: bool = False) -> int:
    """
    Dispatches the initial email invitations for a campaign.
    Supports full immediate send, batch-sized send, and re-dispatch of updated campaigns.
    (Section 30, 31, 51)
    """
    campaign.status = Campaign.Status.ACTIVE
    campaign.started_at = timezone.now()
    campaign.save(update_fields=['status', 'started_at'])

    groups = campaign.groups.all()
    contacts_qs = Contact.objects.filter(
        groups__in=groups,
        unsubscribed=False,
        email_status=Contact.EmailStatus.ACTIVE
    )

    if campaign.initial_recipient_rule == Campaign.InitialRecipientRule.UNUSED_ONLY:
        contacts_qs = contacts_qs.filter(status=Contact.UsageStatus.UNUSED)

    contacts = list(contacts_qs.distinct())
    b_limit = batch_size if (batch_size and batch_size > 0) else len(contacts)

    dispatched = 0
    for idx, c in enumerate(contacts):
        if idx >= b_limit:
            break

        msg, created = CampaignMessage.objects.get_or_create(
            campaign=campaign,
            contact=c,
            message_type=CampaignMessage.MessageType.INITIAL,
            reminder_sequence=0,
            defaults={
                'to_email': c.email,
                'subject': campaign.subject,
                'status': CampaignMessage.Status.QUEUED
            }
        )
        if created:
            process_single_campaign_message(msg.id)
            dispatched += 1
        elif force_resend:
            # Re-dispatch updated campaign content to eligible contact
            msg.subject = campaign.subject
            msg.status = CampaignMessage.Status.QUEUED
            msg.save(update_fields=['subject', 'status'])
            process_single_campaign_message(msg.id)
            dispatched += 1

    # If automated reminders enabled, calculate first reminder schedule
    if hasattr(campaign, 'reminder_config') and campaign.reminder_config.enabled:
        rem_cfg = campaign.reminder_config
        if rem_cfg.interval_unit == ReminderConfiguration.Unit.DAYS:
            delta = timedelta(days=rem_cfg.interval_value)
        else:
            delta = timedelta(weeks=rem_cfg.interval_value)
        rem_cfg.next_run_at = timezone.now() + delta
        rem_cfg.save(update_fields=['next_run_at'])

    return dispatched
