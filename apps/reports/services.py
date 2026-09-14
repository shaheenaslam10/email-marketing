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
