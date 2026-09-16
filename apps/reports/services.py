from django.db.models import Count, Q, Min, Max
from apps.campaigns.models import Campaign, CampaignMessage
from apps.contacts.models import Contact, ContactStatusHistory
from apps.tracking.models import EmailEvent
from apps.reminders.models import ReminderCycle


def get_campaign_full_report(campaign: Campaign) -> dict:
    """
    Compiles full Brevo-style analytics report for a campaign.
    (Sections 55-72)
    """
    messages = campaign.messages.all()
    total_messages = messages.count()

    # Performance counts
    sent_count = messages.filter(status__in=[
        CampaignMessage.Status.SENT, CampaignMessage.Status.DELIVERED,
        CampaignMessage.Status.OPENED, CampaignMessage.Status.CLICKED
    ]).count()

    delivered_count = messages.filter(status__in=[
        CampaignMessage.Status.DELIVERED, CampaignMessage.Status.OPENED,
        CampaignMessage.Status.CLICKED
    ]).count()

    soft_bounce_count = messages.filter(status=CampaignMessage.Status.SOFT_BOUNCE).count()
    hard_bounce_count = messages.filter(status=CampaignMessage.Status.HARD_BOUNCE).count()
    failed_count = messages.filter(status=CampaignMessage.Status.FAILED).count()
    skipped_count = messages.filter(status=CampaignMessage.Status.SKIPPED).count()

    unique_opened_count = messages.filter(opened_at__isnull=False).values('contact_id').distinct().count()
    total_opens_count = EmailEvent.objects.filter(message__campaign=campaign, event_type=EmailEvent.EventType.OPEN).count()

    unique_clicks_count = messages.filter(clicked_at__isnull=False).values('contact_id').distinct().count()
    total_clicks_count = EmailEvent.objects.filter(message__campaign=campaign, event_type=EmailEvent.EventType.CLICK).count()

    unsubscribes_count = EmailEvent.objects.filter(message__campaign=campaign, event_type=EmailEvent.EventType.UNSUBSCRIBE).values('message__contact_id').distinct().count()

    delivery_rate = round((delivered_count / sent_count * 100), 1) if sent_count > 0 else 0.0
    open_rate = round((unique_opened_count / delivered_count * 100), 1) if delivered_count > 0 else 0.0
    click_rate = round((unique_clicks_count / delivered_count * 100), 1) if delivered_count > 0 else 0.0
    ctr = round((unique_clicks_count / unique_opened_count * 100), 1) if unique_opened_count > 0 else 0.0
    unsubscribe_rate = round((unsubscribes_count / delivered_count * 100), 2) if delivered_count > 0 else 0.0

    # Contact status for recipients
    campaign_groups = campaign.groups.all()
    campaign_contacts = Contact.objects.filter(groups__in=campaign_groups).distinct()
    initial_eligible = campaign_contacts.count()
    used_contacts_count = campaign_contacts.filter(status=Contact.UsageStatus.USED).count()
    unused_contacts_count = campaign_contacts.filter(status=Contact.UsageStatus.UNUSED).count()
    completion_rate = round((used_contacts_count / initial_eligible * 100), 1) if initial_eligible > 0 else 0.0

    # Initial messages sent timestamp
    initial_msgs = messages.filter(message_type=CampaignMessage.MessageType.INITIAL)
    initial_sent = initial_msgs.filter(sent_at__isnull=False).count()
    initial_del = initial_msgs.filter(delivered_at__isnull=False).count()
    initial_open = initial_msgs.filter(opened_at__isnull=False).values('contact_id').distinct().count()
    initial_sent_at = initial_msgs.filter(sent_at__isnull=False).aggregate(first_sent=Min('sent_at'))['first_sent'] or campaign.started_at

    # Section 65: Conversion stage breakdown ("Converted after Initial", "Converted after Reminder X")
    stage_breakdown = []
    initial_used_count = campaign_contacts.filter(status=Contact.UsageStatus.USED, reminder_count=0).count()
    stage_breakdown.append({
        'stage': 'Initial Email',
        'is_sent': bool(initial_sent_at),
        'sent_at': initial_sent_at.isoformat() if initial_sent_at else None,
        'scheduled_at': None,
        'became_used': initial_used_count,
    })

    cycles = list(campaign.reminder_config.cycles.order_by('cycle_number')) if hasattr(campaign, 'reminder_config') else []
    
    for cycle in cycles:
        c_msgs = messages.filter(message_type=CampaignMessage.MessageType.REMINDER, reminder_sequence=cycle.cycle_number)
        c_sent_at = cycle.executed_at or c_msgs.filter(sent_at__isnull=False).aggregate(first_sent=Min('sent_at'))['first_sent']
        used_count = campaign_contacts.filter(status=Contact.UsageStatus.USED, reminder_count=cycle.cycle_number).count()
        stage_breakdown.append({
            'stage': f'Reminder {cycle.cycle_number}',
            'is_sent': bool(c_sent_at or cycle.status == ReminderCycle.Status.COMPLETED),
            'sent_at': c_sent_at.isoformat() if c_sent_at else None,
            'scheduled_at': None,
            'became_used': used_count,
        })

    # If next reminder is scheduled and not all max reminders reached
    next_rem_at = campaign.reminder_config.next_run_at if hasattr(campaign, 'reminder_config') else None
    if hasattr(campaign, 'reminder_config') and campaign.reminder_config.enabled and next_rem_at:
        next_cycle_num = len(cycles) + 1
        if next_cycle_num <= campaign.reminder_config.max_reminders and unused_contacts_count > 0:
            stage_breakdown.append({
                'stage': f'Reminder {next_cycle_num}',
                'is_sent': False,
                'sent_at': None,
                'scheduled_at': next_rem_at.isoformat() if next_rem_at else None,
                'became_used': 0,
                'is_upcoming': True,
            })

    # Section 66: Reminder cycles table
    reminder_stages = []
    reminder_stages.append({
        'stage': 'Initial',
        'is_sent': bool(initial_sent_at),
        'sent_at': initial_sent_at.isoformat() if initial_sent_at else None,
        'scheduled_at': None,
        'eligible': initial_eligible,
        'sent': initial_sent,
        'delivered': initial_del,
        'opened': initial_open,
        'became_used': initial_used_count,
        'still_unused': initial_eligible - initial_used_count,
    })

    for cycle in cycles:
        c_msgs = messages.filter(message_type=CampaignMessage.MessageType.REMINDER, reminder_sequence=cycle.cycle_number)
        c_sent = c_msgs.filter(sent_at__isnull=False).count()
        c_del = c_msgs.filter(delivered_at__isnull=False).count()
        c_open = c_msgs.filter(opened_at__isnull=False).values('contact_id').distinct().count()
        c_sent_at = cycle.executed_at or c_msgs.filter(sent_at__isnull=False).aggregate(first_sent=Min('sent_at'))['first_sent']
        became_used = campaign_contacts.filter(status=Contact.UsageStatus.USED, reminder_count=cycle.cycle_number).count()
        still_unused = max(0, cycle.eligible_count - became_used)

        reminder_stages.append({
            'stage': f'Reminder {cycle.cycle_number}',
            'is_sent': bool(c_sent_at or cycle.status == ReminderCycle.Status.COMPLETED),
            'sent_at': c_sent_at.isoformat() if c_sent_at else None,
            'scheduled_at': None,
            'eligible': cycle.eligible_count,
            'sent': c_sent or cycle.sent_count,
            'delivered': c_del or cycle.delivered_count,
            'opened': c_open or cycle.opened_count,
            'became_used': became_used,
            'still_unused': still_unused,
        })

    if hasattr(campaign, 'reminder_config') and campaign.reminder_config.enabled and next_rem_at:
        next_cycle_num = len(cycles) + 1
        if next_cycle_num <= campaign.reminder_config.max_reminders and unused_contacts_count > 0:
            reminder_stages.append({
                'stage': f'Reminder {next_cycle_num} (Scheduled)',
                'is_sent': False,
                'sent_at': None,
                'scheduled_at': next_rem_at.isoformat() if next_rem_at else None,
                'eligible': unused_contacts_count,
                'sent': 0,
                'delivered': 0,
                'opened': 0,
                'became_used': 0,
                'still_unused': unused_contacts_count,
                'is_upcoming': True,
            })

    # Enhanced Link Tracking & Analytics (Requirements 5, 8, 14, 16)
    # Recipient analytics UNION two sources: legacy root-level
    # RecipientLink rows (emails delivered before the /c/ migration) and
    # RECIPIENT-type CampaignTrackingLink rows (all new email links).
    # SHAREABLE links are anonymous and stay in the separate shareable
    # report; they never enter recipient metrics.
    from apps.tracking.models import (
        ShortenedLink, RecipientLink, LinkClickEvent,
        CampaignTrackingLink, CampaignLinkClickEvent,
    )
    from django.db.models import Sum

    RECIPIENT = CampaignTrackingLink.LinkType.RECIPIENT
    shortened_links_qs = ShortenedLink.objects.filter(campaign=campaign)
    recip_links_qs = RecipientLink.objects.filter(campaign=campaign)
    click_events_qs = LinkClickEvent.objects.filter(campaign=campaign)
    recip_c_links_qs = CampaignTrackingLink.objects.filter(
        campaign=campaign, link_type=RECIPIENT)
    recip_c_events_qs = CampaignLinkClickEvent.objects.filter(
        campaign=campaign, link__link_type=RECIPIENT)

    # Human vs bot clicks
    total_link_clicks = click_events_qs.count() + recip_c_events_qs.count()
    if total_link_clicks == 0 and total_clicks_count > 0:
        total_link_clicks = total_clicks_count

    human_clicks_total = (
        click_events_qs.filter(click_type=LinkClickEvent.ClickType.HUMAN).count()
        + recip_c_events_qs.filter(click_type=CampaignLinkClickEvent.ClickType.HUMAN).count()
    )
    bot_clicks_total = (
        click_events_qs.filter(click_type=LinkClickEvent.ClickType.SUSPECTED_BOT).count()
        + recip_c_events_qs.filter(click_type=CampaignLinkClickEvent.ClickType.SUSPECTED_BOT).count()
    )
    unknown_clicks_total = (
        click_events_qs.filter(click_type=LinkClickEvent.ClickType.UNKNOWN).count()
        + recip_c_events_qs.filter(click_type=CampaignLinkClickEvent.ClickType.UNKNOWN).count()
    )
    legacy_human_ids = set(recip_links_qs.filter(
        human_click_count__gt=0).values_list('contact_id', flat=True))
    c_human_ids = set(recip_c_links_qs.filter(
        human_click_count__gt=0).values_list('contact_id', flat=True))
    unique_clickers_human = len(legacy_human_ids | c_human_ids)
    if unique_clickers_human == 0 and unique_clicks_count > 0:
        unique_clickers_human = unique_clicks_count

    legacy_clicked_ids = set(recip_links_qs.filter(
        click_count__gt=0).values_list('contact_id', flat=True))
    c_clicked_ids = set(recip_c_links_qs.filter(
        click_count__gt=0).values_list('contact_id', flat=True))
    total_unique_clicks = len(legacy_clicked_ids | c_clicked_ids)
    if total_unique_clicks == 0:
        total_unique_clicks = unique_clickers_human

    recipients_without_click = max(0, delivered_count - unique_clickers_human)
    link_click_rate = round((unique_clickers_human / delivered_count * 100), 1) if delivered_count > 0 else 0.0

    link_summary = {
        'emails_sent': sent_count,
        'emails_delivered': delivered_count,
        'recipients_with_click': unique_clickers_human,
        'recipients_without_click': recipients_without_click,
        'total_link_clicks': total_link_clicks,
        'unique_link_clicks': unique_clickers_human,
        'unique_click_rate': link_click_rate,
        'human_clicks': human_clicks_total,
        'bot_clicks': bot_clicks_total,
        'unknown_clicks': unknown_clicks_total,
    }

    # Per-link performance table (Requirement 8)
    link_performance = []
    for sl in shortened_links_qs:
        sl_rls = recip_links_qs.filter(shortened_link=sl)
        sl_c_rls = recip_c_links_qs.filter(shortened_link=sl)
        sl_tot = (sl_rls.aggregate(s=Sum('click_count'))['s'] or 0) + \
            (sl_c_rls.aggregate(s=Sum('click_count'))['s'] or 0)
        sl_uniq = len(
            set(sl_rls.filter(click_count__gt=0).values_list('contact_id', flat=True))
            | set(sl_c_rls.filter(click_count__gt=0).values_list('contact_id', flat=True))
        )
        sl_rate = round((sl_uniq / delivered_count * 100), 1) if delivered_count > 0 else 0.0
        link_performance.append({
            'id': sl.id,
            'link_name': sl.link_name or sl.original_url[:40],
            'original_url': sl.original_url,
            'unique_clicks': sl_uniq,
            'total_clicks': sl_tot,
            'click_rate': sl_rate,
        })

    # Device analytics (Requirement 16) - legacy + recipient /c/ events.
    device_counts = {'Desktop': 0, 'Mobile': 0, 'Tablet': 0, 'Unknown': 0}
    for events_qs in (click_events_qs, recip_c_events_qs):
        for d in events_qs.values('device_type').annotate(cnt=Count('id')):
            dt = d.get('device_type') or 'Unknown'
            if dt in device_counts:
                device_counts[dt] += d['cnt']
            else:
                device_counts['Unknown'] += d['cnt']

    # Browser analytics (Requirement 16) - legacy + recipient /c/ events.
    browser_counts = {'Chrome': 0, 'Edge': 0, 'Safari': 0, 'Firefox': 0, 'Other': 0}
    for events_qs in (click_events_qs, recip_c_events_qs):
        for b in events_qs.values('browser').annotate(cnt=Count('id')):
            br = b.get('browser') or 'Other'
            if br in browser_counts:
                browser_counts[br] += b['cnt']
            else:
                browser_counts['Other'] += b['cnt']

    # Click timeline (by date) - legacy + recipient /c/ events combined.
    date_counts = {}
    for events_qs in (click_events_qs, recip_c_events_qs):
        for ev in events_qs.extra({'click_date': "date(clicked_at)"}).values('click_date').annotate(cnt=Count('id')).order_by('click_date'):
            key = str(ev.get('click_date', ''))
            date_counts[key] = date_counts.get(key, 0) + ev.get('cnt', 0)
    click_timeline = [
        {'date': date_key, 'clicks': date_counts[date_key]}
        for date_key in sorted(date_counts)[:14]
    ]

    # Legacy link_clicks fallback for compatibility
    link_clicks_qs = EmailEvent.objects.filter(
        message__campaign=campaign,
        event_type=EmailEvent.EventType.CLICK
    ).values('url_clicked').annotate(
        total_clicks=Count('id'),
        unique_clicks=Count('message__contact_id', distinct=True)
    ).order_by('-total_clicks')

    # Still unused contacts summary (Section 69)
    still_unused_qs = campaign_contacts.filter(status=Contact.UsageStatus.UNUSED)[:100]
    still_unused_contacts = []
    for c in still_unused_qs:
        opened = messages.filter(contact=c, opened_at__isnull=False).exists()
        still_unused_contacts.append({
            'id': c.id,
            'name': c.name,
            'email': c.email,
            'phone': c.phone_number,
            'job_id': c.job_id,
            'emails_sent': messages.filter(contact=c).count(),
            'reminders_count': c.reminder_count,
            'last_reminder_at': c.last_reminder_sent_at,
            'opened': opened,
        })

    return {
        'campaign': {
            'id': campaign.id,
            'name': campaign.name,
            'subject': campaign.subject,
            'status': campaign.status,
            'campaign_type': campaign.campaign_type,
            'sender_name': campaign.sender.name,
            'sender_email': campaign.sender.email,
            'started_at': campaign.started_at,
            'completed_at': campaign.completed_at,
            'next_reminder_at': campaign.reminder_config.next_run_at if hasattr(campaign, 'reminder_config') else None,
            'reminder_enabled': campaign.reminder_config.enabled if hasattr(campaign, 'reminder_config') else False,
        },
        'kpi': {
            'initial_eligible': initial_eligible,
            'sent_count': sent_count,
            'delivered_count': delivered_count,
            'delivery_rate': delivery_rate,
            'unique_opened_count': unique_opened_count,
            'total_opens_count': total_opens_count,
            'open_rate': open_rate,
            'unique_clicks_count': unique_clicks_count,
            'total_clicks_count': total_clicks_count,
            'click_rate': click_rate,
            'ctr': ctr,
            'unsubscribes_count': unsubscribes_count,
            'unsubscribe_rate': unsubscribe_rate,
            'soft_bounce_count': soft_bounce_count,
            'hard_bounce_count': hard_bounce_count,
            'failed_count': failed_count,
            'skipped_count': skipped_count,
        },
        'survey_conversion': {
            'used_count': used_contacts_count,
            'unused_count': unused_contacts_count,
            'completion_rate': completion_rate,
            'stage_breakdown': stage_breakdown,
        },
        'deliverability_table': [
            {'status': 'Sent', 'count': sent_count, 'rate': 100.0},
            {'status': 'Delivered', 'count': delivered_count, 'rate': delivery_rate},
            {'status': 'Soft Bounce', 'count': soft_bounce_count, 'rate': round(soft_bounce_count / sent_count * 100, 1) if sent_count else 0.0},
            {'status': 'Hard Bounce', 'count': hard_bounce_count, 'rate': round(hard_bounce_count / sent_count * 100, 1) if sent_count else 0.0},
            {'status': 'Failed', 'count': failed_count, 'rate': round(failed_count / sent_count * 100, 1) if sent_count else 0.0},
        ],
        'reminder_stages': reminder_stages,
        'link_clicks': list(link_clicks_qs),
        'link_summary': link_summary,
        'link_performance': link_performance,
        'device_analytics': device_counts,
        'browser_analytics': browser_counts,
        'click_timeline': click_timeline,
        'still_unused_contacts': still_unused_contacts,
    }


# ---------------------------------------------------------------------------
# Shared recipient-lifecycle rows.
#
# Single source of truth behind the link-recipients API, its CSV/XLSX
# exports, and the multi-campaign Excel workbook export. Definitions here
# intentionally mirror the report UI:
#   * contacts = campaign groups UNION messaged contacts
#   * clicks   = legacy RecipientLink + RECIPIENT-type /c/ links
#   * sent/opened = earliest INITIAL/REMINDER message timestamps
#                   (TEST messages excluded)
#   * completed = Contact.status USED (ODK completion)
#   * reminder verdict = apps.reminders.services.reminder_eligibility
#     (open/click state NEVER gates eligibility)
# ---------------------------------------------------------------------------

# Recipient/lifecycle selector values for the Excel export dialog (and the
# optional ``lifecycle`` GET parameter on link-recipients/). Unknown values
# behave as 'all'.
LIFECYCLE_ALL = 'all'
LIFECYCLE_COMPLETED = 'completed'
LIFECYCLE_INCOMPLETE = 'incomplete'
LIFECYCLE_OPENED = 'opened'
LIFECYCLE_NOT_OPENED = 'not_opened'
LIFECYCLE_CLICKED = 'clicked'
LIFECYCLE_NOT_CLICKED = 'not_clicked'
LIFECYCLE_CLICKED_NOT_COMPLETED = 'clicked_not_completed'
LIFECYCLE_UNENGAGED_INCOMPLETE = 'unengaged_incomplete'  # never opened + never clicked + incomplete
LIFECYCLE_REMINDER_ELIGIBLE = 'reminder_eligible'
LIFECYCLE_REMINDER_SUPPRESSED = 'reminder_suppressed'

LIFECYCLE_LABELS = {
    LIFECYCLE_ALL: 'All recipients',
    LIFECYCLE_COMPLETED: 'Completed',
    LIFECYCLE_INCOMPLETE: 'Incomplete / UNUSED',
    LIFECYCLE_OPENED: 'Opened',
    LIFECYCLE_NOT_OPENED: 'Not opened',
    LIFECYCLE_CLICKED: 'Clicked',
    LIFECYCLE_NOT_CLICKED: 'Not clicked',
    LIFECYCLE_CLICKED_NOT_COMPLETED: 'Clicked but not completed',
    LIFECYCLE_UNENGAGED_INCOMPLETE: 'Never opened/clicked, incomplete',
    LIFECYCLE_REMINDER_ELIGIBLE: 'Reminder eligible',
    LIFECYCLE_REMINDER_SUPPRESSED: 'Reminder suppressed',
}


def _recipient_link_label(rl):
    """Display name for a legacy RecipientLink or RECIPIENT /c/ link."""
    sl = getattr(rl, 'shortened_link', None)
    if sl is not None:
        return sl.link_name or sl.original_url[:35]
    return getattr(rl, 'name', '') or 'Tracked Link'


def _recipient_link_destination(rl):
    """Destination URL for a legacy RecipientLink or RECIPIENT /c/ link."""
    sl = getattr(rl, 'shortened_link', None)
    if sl is not None:
        return sl.original_url
    return getattr(rl, 'destination_url', '') or ''


def parse_report_datetime(value):
    """Leniently parse a report date filter into an aware datetime.

    Accepts ISO datetimes and plain YYYY-MM-DD dates; naive values are
    assumed in the current time zone. Returns None when unparseable so
    callers can ignore bad input (never 500 on a filter typo).
    """
    from datetime import datetime
    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime

    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    dt = parse_datetime(text)
    if dt is None:
        day = parse_date(text)
        if day is None:
            return None
        dt = datetime(day.year, day.month, day.day)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def get_recipient_lifecycle_rows(campaign, filters=None):
    """Build recipient lifecycle rows for a campaign.

    Shared by the link-recipients API (JSON + CSV + XLSX) and the Excel
    workbook export so every surface reports identical numbers.

    ``filters`` keys (all optional): status, contact_status, link_id,
    group_id, job_id, search, date_from, date_to, lifecycle.
    Returns rows sorted clicked-first, then most clicks, then name.
    """
    from collections import defaultdict
    from apps.tracking.models import (
        RecipientLink, CampaignTrackingLink,
    )
    from apps.reminders.services import reminder_eligibility

    filters = filters or {}
    status_filter = str(filters.get('status', 'all') or 'all').strip().lower()
    contact_status_filter = str(filters.get('contact_status', 'all') or 'all').strip().upper()
    link_id_filter = str(filters.get('link_id', '') or '').strip()
    group_id_filter = str(filters.get('group_id', '') or '').strip()
    job_id_filter = str(filters.get('job_id', '') or '').strip()
    search_query = str(filters.get('search', '') or '').strip()
    date_from = parse_report_datetime(filters.get('date_from', ''))
    date_to = parse_report_datetime(filters.get('date_to', ''))
    lifecycle = str(filters.get('lifecycle', 'all') or 'all').strip().lower()

    # All contacts belonging to campaign groups or who received messages
    campaign_groups = campaign.groups.all()
    contacts_qs = Contact.objects.filter(
        Q(groups__in=campaign_groups) | Q(campaign_messages__campaign=campaign)
    ).distinct()

    if contact_status_filter in ('USED', 'UNUSED'):
        contacts_qs = contacts_qs.filter(status=contact_status_filter)

    if group_id_filter and group_id_filter.isdigit():
        contacts_qs = contacts_qs.filter(groups__id=int(group_id_filter))

    if job_id_filter:
        contacts_qs = contacts_qs.filter(job_id__icontains=job_id_filter)

    if search_query:
        contacts_qs = contacts_qs.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(job_id__icontains=search_query)
        )

    # Recipient links for this campaign: legacy root-level RecipientLink
    # rows plus RECIPIENT-type /c/ links. SHAREABLE links are anonymous
    # and never enter this per-recipient view.
    rl_qs = RecipientLink.objects.filter(campaign=campaign).select_related('shortened_link', 'contact')
    rc_qs = CampaignTrackingLink.objects.filter(
        campaign=campaign,
        link_type=CampaignTrackingLink.LinkType.RECIPIENT,
    ).select_related('shortened_link', 'contact')
    if link_id_filter and link_id_filter.isdigit():
        rl_qs = rl_qs.filter(shortened_link_id=int(link_id_filter))
        rc_qs = rc_qs.filter(shortened_link_id=int(link_id_filter))

    # Index recipient links by contact_id (two queries total)
    contact_links_map = defaultdict(list)
    for rl in rl_qs:
        contact_links_map[rl.contact_id].append(rl)
    for rl in rc_qs:
        contact_links_map[rl.contact_id].append(rl)

    # Index lifecycle messages by contact (single query): earliest
    # INITIAL/REMINDER send + delivery + open timestamps per recipient.
    # TEST messages are excluded so test mail never pollutes lifecycle state.
    contact_msgs_map = defaultdict(list)
    for m in campaign.messages.filter(
        message_type__in=[
            CampaignMessage.MessageType.INITIAL,
            CampaignMessage.MessageType.REMINDER,
        ]
    ).only('contact_id', 'sent_at', 'delivered_at', 'opened_at'):
        contact_msgs_map[m.contact_id].append(m)

    # Build combined recipient rows
    rows = []
    for contact in contacts_qs:
        rlinks = contact_links_map.get(contact.id, [])
        total_clicks = sum(rl.click_count for rl in rlinks)
        human_clicks = sum(rl.human_click_count for rl in rlinks)
        bot_clicks = sum(rl.bot_click_count for rl in rlinks)

        # Dates
        first_clicks = [rl.first_clicked_at for rl in rlinks if rl.first_clicked_at]
        last_clicks = [rl.last_clicked_at for rl in rlinks if rl.last_clicked_at]
        first_click_at = min(first_clicks) if first_clicks else None
        last_click_at = max(last_clicks) if last_clicks else None

        # Filter by date range if provided (first click >= from,
        # last click <= to). Unparseable input is ignored.
        if date_from and (not first_click_at or first_click_at < date_from):
            continue
        if date_to and (not last_click_at or last_click_at > date_to):
            continue

        # Status filter
        if status_filter == 'clicked' and total_clicks == 0:
            continue
        if status_filter == 'not_clicked' and total_clicks > 0:
            continue
        if status_filter == 'clicked_once' and total_clicks != 1:
            continue
        if status_filter == 'clicked_multiple' and total_clicks <= 1:
            continue

        # Link details summary
        link_names = [_recipient_link_label(rl) for rl in rlinks]
        display_link_name = ", ".join(link_names) if link_names else "No Link Generated"
        sample_short_url = rlinks[0].short_url if rlinks else ""
        sample_orig_url = _recipient_link_destination(rlinks[0]) if rlinks else ""

        # Click type classification
        if bot_clicks > 0 and human_clicks == 0:
            click_type_str = "Suspected Bot"
        elif human_clicks > 0:
            click_type_str = "Human"
        elif total_clicks > 0:
            click_type_str = "Unknown"
        else:
            click_type_str = "None"

        # Lifecycle: email sent/delivered/opened, ODK completion, reminder verdict.
        msgs = contact_msgs_map.get(contact.id, [])
        sent_times = [m.sent_at for m in msgs if m.sent_at]
        delivered_times = [m.delivered_at for m in msgs if getattr(m, 'delivered_at', None)]
        open_times = [m.opened_at for m in msgs if m.opened_at]
        email_sent_at = min(sent_times) if sent_times else None
        email_delivered_at = min(delivered_times) if delivered_times else None
        email_opened_at = min(open_times) if open_times else None
        completed_at = contact.odk_submitted_at
        verdict = reminder_eligibility(contact, campaign)

        has_sent = email_sent_at is not None
        has_delivered = email_delivered_at is not None
        has_opened = email_opened_at is not None
        has_clicked = total_clicks > 0
        is_completed = contact.status == Contact.UsageStatus.USED

        # Lifecycle selector (Excel dialog). AND-combined with the
        # status/contact_status filters above; unknown values = all.
        if lifecycle == LIFECYCLE_COMPLETED and not is_completed:
            continue
        if lifecycle == LIFECYCLE_INCOMPLETE and is_completed:
            continue
        if lifecycle == LIFECYCLE_OPENED and not has_opened:
            continue
        if lifecycle == LIFECYCLE_NOT_OPENED and has_opened:
            continue
        if lifecycle == LIFECYCLE_CLICKED and not has_clicked:
            continue
        if lifecycle == LIFECYCLE_NOT_CLICKED and has_clicked:
            continue
        if lifecycle == LIFECYCLE_CLICKED_NOT_COMPLETED and not (has_clicked and not is_completed):
            continue
        if lifecycle == LIFECYCLE_UNENGAGED_INCOMPLETE and not (
            not has_opened and not has_clicked and not is_completed
        ):
            continue
        if lifecycle == LIFECYCLE_REMINDER_ELIGIBLE and not verdict['eligible']:
            continue
        if lifecycle == LIFECYCLE_REMINDER_SUPPRESSED and verdict['eligible']:
            continue

        last_reminder_at = getattr(contact, 'last_reminder_sent_at', None)
        rows.append({
            'contact_id': contact.id,
            'name': contact.name or f"{contact.first_name} {contact.last_name}".strip() or contact.email,
            'email': contact.email,
            'phone': contact.phone_number or '-',
            'job_id': contact.job_id or '-',
            'contact_status': contact.status,  # USED / UNUSED
            'email_sent_at': email_sent_at.strftime('%d %b %Y, %I:%M %p') if email_sent_at else '-',
            'email_delivered_at': email_delivered_at.strftime('%d %b %Y, %I:%M %p') if email_delivered_at else '-',
            'email_opened_at': email_opened_at.strftime('%d %b %Y, %I:%M %p') if email_opened_at else '-',
            'email_sent_dt': email_sent_at,
            'email_delivered_dt': email_delivered_at,
            'email_opened_dt': email_opened_at,
            'has_sent': has_sent,
            'has_delivered': has_delivered,
            'has_opened': has_opened,
            'has_clicked': has_clicked,
            'tracking_token': rlinks[0].tracking_token if rlinks else '',
            'link_status': 'Clicked' if has_clicked else 'Not Clicked',
            'first_click': first_click_at.strftime('%d %b %Y, %I:%M %p') if first_click_at else '-',
            'last_click': last_click_at.strftime('%d %b %Y, %I:%M %p') if last_click_at else '-',
            'first_click_dt': first_click_at,
            'last_click_dt': last_click_at,
            'total_clicks': total_clicks,
            'human_clicks': human_clicks,
            'bot_clicks': bot_clicks,
            'click_type': click_type_str,
            'completed_at': completed_at.strftime('%d %b %Y, %I:%M %p') if completed_at else '-',
            'completed_dt': completed_at,
            'odk_submission_id': contact.odk_submission_id or '',
            'reminder_eligible': verdict['eligible'],
            'reminder_reason': verdict['reason'],
            'reminders_sent': getattr(contact, 'reminder_count', 0) or 0,
            'last_reminder_at': last_reminder_at.strftime('%d %b %Y, %I:%M %p') if last_reminder_at else '-',
            'link_name': display_link_name,
            'short_url': sample_short_url,
            'original_url': sample_orig_url,
        })

    # Sort: Clicked first, then most clicks, then name
    rows.sort(key=lambda r: (r['total_clicks'], r['name']), reverse=True)
    return rows


def get_click_activity_rows(campaign, link_id=None, date_from=None,
                            date_to=None, contact_ids=None, limit=None):
    """Recipient click events for a campaign, newest first.

    Unions legacy root-level LinkClickEvent rows with RECIPIENT-type /c/
    visits. Shareable (anonymous) visits are excluded; they belong to the
    shareable-links report. IP addresses are intentionally excluded
    (privacy; staff can use Django admin).

    Returns (rows, total_events) where total_events ignores ``limit``.
    """
    from apps.tracking.models import (
        LinkClickEvent, CampaignTrackingLink, CampaignLinkClickEvent,
    )
    from apps.tracking.utils import build_campaign_short_url

    df = parse_report_datetime(date_from) if date_from else None
    dt = parse_report_datetime(date_to) if date_to else None
    selected_sl_id = None
    if link_id is not None and str(link_id).strip().isdigit():
        selected_sl_id = int(str(link_id).strip())

    legacy_qs = LinkClickEvent.objects.filter(
        campaign=campaign
    ).select_related('recipient_link__shortened_link', 'contact')
    new_qs = CampaignLinkClickEvent.objects.filter(
        campaign=campaign,
        link__link_type=CampaignTrackingLink.LinkType.RECIPIENT,
    ).select_related('link__shortened_link', 'contact')
    if selected_sl_id:
        legacy_qs = legacy_qs.filter(
            recipient_link__shortened_link_id=selected_sl_id)
        new_qs = new_qs.filter(link__shortened_link_id=selected_sl_id)
    if contact_ids is not None:
        legacy_qs = legacy_qs.filter(contact_id__in=contact_ids)
        new_qs = new_qs.filter(contact_id__in=contact_ids)
    if df:
        legacy_qs = legacy_qs.filter(clicked_at__gte=df)
        new_qs = new_qs.filter(clicked_at__gte=df)
    if dt:
        legacy_qs = legacy_qs.filter(clicked_at__lte=dt)
        new_qs = new_qs.filter(clicked_at__lte=dt)

    total_events = legacy_qs.count() + new_qs.count()

    def click_type_label(ct):
        return {
            'HUMAN': 'Human',
            'SUSPECTED_BOT': 'Suspected Bot',
            'UNKNOWN': 'Unknown',
        }.get(ct, 'Unknown')

    def contact_display(contact):
        if not contact:
            return '-', '-'
        name = contact.name or (
            (contact.first_name or '') + ' ' + (contact.last_name or '')
        ).strip() or contact.email
        return name, contact.email

    rows = []
    legacy_list = legacy_qs.order_by('-clicked_at', '-id')
    new_list = new_qs.order_by('-clicked_at', '-id')
    if limit is not None:
        legacy_list = legacy_list[:limit]
        new_list = new_list[:limit]
    for e in legacy_list:
        sl = e.recipient_link.shortened_link
        name, email = contact_display(e.contact)
        rows.append({
            '_sort': (e.clicked_at, 'L', e.id),
            'clicked_at': e.clicked_at.isoformat(),
            'clicked_dt': e.clicked_at,
            'contact_id': e.contact_id,
            'contact_name': name,
            'contact_email': email,
            'contact_phone': e.contact.phone_number if e.contact else '-',
            'tracking_token': e.recipient_link.tracking_token,
            'link_name': sl.link_name or 'Tracked Link',
            'short_url': e.recipient_link.short_url,
            'destination_url': sl.original_url,
            'click_type': click_type_label(e.click_type),
            'browser': e.browser or 'Unknown',
            'operating_system': e.operating_system or 'Unknown',
            'device_type': e.device_type or 'Unknown',
            'referrer': e.referrer or '',
        })
    for e in new_list:
        sl = e.link.shortened_link
        name, email = contact_display(e.contact)
        rows.append({
            '_sort': (e.clicked_at, 'C', e.id),
            'clicked_at': e.clicked_at.isoformat(),
            'clicked_dt': e.clicked_at,
            'contact_id': e.contact_id,
            'contact_name': name,
            'contact_email': email,
            'contact_phone': e.contact.phone_number if e.contact else '-',
            'tracking_token': e.link.tracking_token,
            'link_name': (sl.link_name if sl else None) or e.link.name or 'Tracked Link',
            # Derived from the current base: the stored short_url
            # may have been minted under another environment.
            'short_url': build_campaign_short_url(e.link.tracking_token),
            'destination_url': (e.link.destination_url or (sl.original_url if sl else '') or ''),
            'click_type': click_type_label(e.click_type),
            'browser': e.browser or 'Unknown',
            'operating_system': e.operating_system or 'Unknown',
            'device_type': e.device_type or 'Unknown',
            'referrer': e.referrer or '',
        })
    rows.sort(key=lambda r: r['_sort'], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    for r in rows:
        del r['_sort']
    return rows, total_events


def get_reminder_activity_rows(campaign, contact_ids=None):
    """REMINDER message records for a campaign (existing records only).

    One row per reminder message: sequence, delivery status, timestamps,
    and the suppression/skip reason. No records are invented here; rows
    come straight from CampaignMessage.
    """
    msgs = campaign.messages.filter(
        message_type=CampaignMessage.MessageType.REMINDER
    ).select_related('contact').order_by('reminder_sequence', 'sent_at', 'id')
    if contact_ids is not None:
        msgs = msgs.filter(contact_id__in=contact_ids)

    sent_statuses = {
        CampaignMessage.Status.SENT, CampaignMessage.Status.DELIVERED,
        CampaignMessage.Status.OPENED, CampaignMessage.Status.CLICKED,
    }
    rows = []
    for m in msgs:
        c = m.contact
        if m.status in sent_statuses:
            status_label = 'Sent'
        elif m.status == CampaignMessage.Status.SKIPPED:
            status_label = 'Suppressed'
        elif m.status == CampaignMessage.Status.FAILED:
            status_label = 'Failed'
        elif m.status == CampaignMessage.Status.QUEUED:
            status_label = 'Queued'
        else:
            status_label = m.status
        rows.append({
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'contact_id': c.id if c else None,
            'contact_name': (c.name if c else None) or (m.to_email or 'Unknown Contact'),
            'contact_email': m.to_email or (c.email if c else ''),
            'sequence': m.reminder_sequence,
            'status': m.status,
            'status_label': status_label,
            'sent_dt': m.sent_at,
            'reason': m.skip_reason or m.error_message or '',
        })
    return rows


def get_visible_campaigns(user):
    """Campaigns the user may list and export.

    Mirrors CampaignViewSet's base queryset (the /campaigns/ listing
    page and /api/campaigns/): currently every authenticated user sees
    all campaigns. If per-user scoping is ever added to the listing,
    apply it here too and every export path inherits it.
    """
    from apps.campaigns.models import Campaign
    if user is None or not getattr(user, 'is_authenticated', False):
        return Campaign.objects.none()
    return Campaign.objects.all().order_by('id')


def sanitize_filename_part(text, max_length=40):
    """Make a string safe for use inside a download filename."""
    import re
    cleaned = re.sub(r'[^A-Za-z0-9]+', '_', str(text or '')).strip('_')
    if not cleaned:
        cleaned = 'Report'
    return cleaned[:max_length]


def build_report_filename(campaign_names, lifecycle='all', search='', ext='xlsx',
                            scope_all=False):
    """Dynamic sanitized workbook filename, e.g.
    Campaign_Report_Pulse_V1_Pulse_V2_2026-09-16.xlsx or
    Campaign_Report_Incomplete_2026-09-16.xlsx.
    """
    from django.utils import timezone as dj_timezone
    today = dj_timezone.localdate().isoformat()
    if scope_all:
        return f'Campaign_Report_All_Campaigns_{today}.{ext}'
    parts = [sanitize_filename_part(n) for n in (campaign_names or [])]
    parts = [p for p in parts if p]
    label = (LIFECYCLE_LABELS.get(str(lifecycle or 'all').lower(), '') or '').strip()
    if str(lifecycle or 'all').lower() not in ('all', '') and label and label.lower() != 'all recipients':
        parts.append(sanitize_filename_part(label))
    if (search or '').strip():
        parts.append('Filtered')
    if not parts:
        parts = ['Campaigns']
    stem = 'Campaign_Report_' + '_'.join(parts)
    if len(stem) > 120:
        stem = stem[:120].rstrip('_')
    return f'{stem}_{today}.{ext}'


def reminder_status_label(row):
    """Human-readable reminder state for a lifecycle row.

    Shared by the Excel workbook and the CSV export so both surfaces
    describe reminder state identically.
    """
    if not row['reminder_eligible']:
        return 'Suppressed'
    if row['reminders_sent'] > 0:
        return 'Sent (%d)' % row['reminders_sent']
    return 'Pending'
