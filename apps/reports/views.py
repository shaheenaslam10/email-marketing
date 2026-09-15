from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from apps.campaigns.models import Campaign, CampaignMessage
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup
from apps.reminders.models import ReminderConfiguration
from apps.tracking.models import EmailEvent
from django.db.models import Q
from .services import get_campaign_full_report
from .exporters import export_campaign_xlsx, export_campaign_csv, export_campaign_pdf


class CampaignReportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        report_data = get_campaign_full_report(campaign)
        return Response(report_data)


class CampaignReportMessagesView(APIView):
    """
    Drill-down API returning contact messages for a campaign filtered by delivery status.
    Supports status in: Sent, Delivered, Soft Bounce, Hard Bounce, Failed, Opened, Clicked.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        status_filter = request.GET.get('status', 'Sent').strip()
        search_query = request.GET.get('search', '').strip()
        stage_filter = request.GET.get('stage', '').strip()

        messages = campaign.messages.select_related('contact').all()

        norm_status = status_filter.lower().replace(' ', '_')
        if norm_status == 'sent':
            messages = messages.filter(status__in=[
                CampaignMessage.Status.SENT, CampaignMessage.Status.DELIVERED,
                CampaignMessage.Status.OPENED, CampaignMessage.Status.CLICKED
            ])
        elif norm_status == 'delivered':
            messages = messages.filter(status__in=[
                CampaignMessage.Status.DELIVERED, CampaignMessage.Status.OPENED,
                CampaignMessage.Status.CLICKED
            ])
        elif norm_status == 'soft_bounce':
            messages = messages.filter(status=CampaignMessage.Status.SOFT_BOUNCE)
        elif norm_status == 'hard_bounce':
            messages = messages.filter(status=CampaignMessage.Status.HARD_BOUNCE)
        elif norm_status == 'failed':
            messages = messages.filter(status=CampaignMessage.Status.FAILED)
        elif norm_status == 'opened':
            messages = messages.filter(opened_at__isnull=False)
        elif norm_status == 'clicked':
            messages = messages.filter(clicked_at__isnull=False)
        elif norm_status != 'all':
            messages = messages.filter(status__iexact=status_filter)

        if stage_filter:
            if stage_filter.lower() == 'initial':
                messages = messages.filter(message_type=CampaignMessage.MessageType.INITIAL)
            elif 'reminder' in stage_filter.lower():
                parts = stage_filter.split()
                if len(parts) > 1 and parts[1].isdigit():
                    messages = messages.filter(message_type=CampaignMessage.MessageType.REMINDER, reminder_sequence=int(parts[1]))

        if search_query:
            messages = messages.filter(
                Q(contact__first_name__icontains=search_query) |
                Q(contact__last_name__icontains=search_query) |
                Q(contact__email__icontains=search_query) |
                Q(contact__job_id__icontains=search_query) |
                Q(to_email__icontains=search_query)
            )

        messages = messages.order_by('-sent_at', '-id')
        total_count = messages.count()

        results = []
        for m in messages[:300]:
            c = m.contact
            stage_name = 'Initial Email' if m.message_type == CampaignMessage.MessageType.INITIAL else f'Reminder {m.reminder_sequence}'
            results.append({
                'id': m.id,
                'contact_id': c.id if c else None,
                'name': c.name if c else (m.to_email or 'Unknown Contact'),
                'email': m.to_email or (c.email if c else ''),
                'job_id': c.job_id if c else '',
                'stage': stage_name,
                'status': m.status,
                'contact_status': c.status if c else 'UNKNOWN',
                'sent_at': m.sent_at.isoformat() if m.sent_at else None,
                'delivered_at': m.delivered_at.isoformat() if m.delivered_at else None,
                'opened_at': m.opened_at.isoformat() if m.opened_at else None,
                'error_message': m.error_message or m.skip_reason or '',
            })

        return Response({
            'status': status_filter,
            'total_count': total_count,
            'messages': results
        })


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


class CampaignReportLinkRecipientsView(APIView):
    """
    Recipient-Level Link Tracking Report (Requirements 6, 8, 9, 14).
    Returns detailed recipient activity with multi-criteria filters, search,
    custom pagination (200 | 400 | 600 | 1000 | All), and CSV / Excel export.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        from apps.tracking.models import RecipientLink, ShortenedLink, LinkClickEvent
        import csv
        import io
        from django.http import HttpResponse

        status_filter = request.GET.get('status', 'all').strip().lower()
        contact_status_filter = request.GET.get('contact_status', 'all').strip().upper()
        link_id_filter = request.GET.get('link_id', '').strip()
        group_id_filter = request.GET.get('group_id', '').strip()
        job_id_filter = request.GET.get('job_id', '').strip()
        search_query = request.GET.get('search', '').strip()
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        export_fmt = request.GET.get('export', '').strip().lower()

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

        # Get recipient links for this campaign: legacy root-level
        # RecipientLink rows plus RECIPIENT-type /c/ links. SHAREABLE
        # links are anonymous and never enter this per-recipient view.
        from apps.tracking.models import CampaignTrackingLink
        rl_qs = RecipientLink.objects.filter(campaign=campaign).select_related('shortened_link', 'contact')
        rc_qs = CampaignTrackingLink.objects.filter(
            campaign=campaign,
            link_type=CampaignTrackingLink.LinkType.RECIPIENT,
        ).select_related('shortened_link', 'contact')
        if link_id_filter and link_id_filter.isdigit():
            rl_qs = rl_qs.filter(shortened_link_id=int(link_id_filter))
            rc_qs = rc_qs.filter(shortened_link_id=int(link_id_filter))

        # Index recipient links by contact_id
        from collections import defaultdict
        from apps.reminders.services import reminder_eligibility
        contact_links_map = defaultdict(list)
        for rl in rl_qs:
            contact_links_map[rl.contact_id].append(rl)
        for rl in rc_qs:
            contact_links_map[rl.contact_id].append(rl)

        # Index lifecycle messages by contact (single query): earliest
        # INITIAL/REMINDER send + open timestamps per recipient. TEST
        # messages are excluded so test mail never pollutes lifecycle state.
        contact_msgs_map = defaultdict(list)
        for m in campaign.messages.filter(
            message_type__in=[
                CampaignMessage.MessageType.INITIAL,
                CampaignMessage.MessageType.REMINDER,
            ]
        ).only('contact_id', 'sent_at', 'opened_at'):
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

            # Filter by date range if provided
            if date_from:
                try:
                    df = timezone.datetime.fromisoformat(date_from)
                    if not first_click_at or first_click_at < df:
                        continue
                except Exception:
                    pass
            if date_to:
                try:
                    dt = timezone.datetime.fromisoformat(date_to)
                    if not last_click_at or last_click_at > dt:
                        continue
                except Exception:
                    pass

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

            # Lifecycle: email sent/opened, ODK completion, reminder verdict.
            msgs = contact_msgs_map.get(contact.id, [])
            sent_times = [m.sent_at for m in msgs if m.sent_at]
            open_times = [m.opened_at for m in msgs if m.opened_at]
            email_sent_at = min(sent_times) if sent_times else None
            email_opened_at = min(open_times) if open_times else None
            completed_at = contact.odk_submitted_at
            verdict = reminder_eligibility(contact, campaign)

            rows.append({
                'contact_id': contact.id,
                'name': contact.name or f"{contact.first_name} {contact.last_name}".strip() or contact.email,
                'email': contact.email,
                'phone': contact.phone_number or '-',
                'job_id': contact.job_id or '-',
                'contact_status': contact.status,  # USED / UNUSED
                'email_sent_at': email_sent_at.strftime('%d %b %Y, %I:%M %p') if email_sent_at else '-',
                'email_opened_at': email_opened_at.strftime('%d %b %Y, %I:%M %p') if email_opened_at else '-',
                'tracking_token': rlinks[0].tracking_token if rlinks else '',
                'link_status': 'Clicked' if total_clicks > 0 else 'Not Clicked',
                'first_click': first_click_at.strftime('%d %b %Y, %I:%M %p') if first_click_at else '-',
                'last_click': last_click_at.strftime('%d %b %Y, %I:%M %p') if last_click_at else '-',
                'total_clicks': total_clicks,
                'human_clicks': human_clicks,
                'bot_clicks': bot_clicks,
                'click_type': click_type_str,
                'completed_at': completed_at.strftime('%d %b %Y, %I:%M %p') if completed_at else '-',
                'odk_submission_id': contact.odk_submission_id or '',
                'reminder_eligible': verdict['eligible'],
                'reminder_reason': verdict['reason'],
                'link_name': display_link_name,
                'short_url': sample_short_url,
                'original_url': sample_orig_url,
            })

        # Sort: Clicked first, then most clicks, then name
        rows.sort(key=lambda r: (r['total_clicks'], r['name']), reverse=True)
        total_count = len(rows)

        # Handle CSV export
        if export_fmt == 'csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="campaign_{campaign.id}_link_tracking.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Name', 'Email', 'Phone', 'JobID', 'Survey Status',
                'Email Sent', 'Email Opened', 'Link Status', 'Tracking Token',
                'First Click', 'Last Click', 'Total Clicks', 'Human Clicks',
                'Bot Clicks', 'Click Type', 'Completed At', 'ODK Submission',
                'Reminder Eligible', 'Reminder Reason',
                'Link Name', 'Short URL', 'Original Destination'
            ])
            for r in rows:
                writer.writerow([
                    r['name'], r['email'], r['phone'], r['job_id'], r['contact_status'],
                    r['email_sent_at'], r['email_opened_at'], r['link_status'], r['tracking_token'],
                    r['first_click'], r['last_click'], r['total_clicks'], r['human_clicks'],
                    r['bot_clicks'], r['click_type'], r['completed_at'], r['odk_submission_id'],
                    'Yes' if r['reminder_eligible'] else 'No', r['reminder_reason'],
                    r['link_name'], r['short_url'], r['original_url']
                ])
            return response

        # Handle Excel (XLSX) export
        if export_fmt in ('xlsx', 'excel'):
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Link Tracking Report"

            headers = [
                'Name', 'Email', 'Phone', 'JobID', 'Survey Status',
                'Email Sent', 'Email Opened', 'Link Status', 'Tracking Token',
                'First Click', 'Last Click', 'Total Clicks', 'Human Clicks',
                'Bot Clicks', 'Click Type', 'Completed At', 'ODK Submission',
                'Reminder Eligible', 'Reminder Reason',
                'Link Name', 'Short URL', 'Original Destination'
            ]
            ws.append(headers)

            # Style header row
            header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
            header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

            # Append rows
            for r in rows:
                ws.append([
                    r['name'], r['email'], r['phone'], r['job_id'], r['contact_status'],
                    r['email_sent_at'], r['email_opened_at'], r['link_status'], r['tracking_token'],
                    r['first_click'], r['last_click'], r['total_clicks'], r['human_clicks'],
                    r['bot_clicks'], r['click_type'], r['completed_at'], r['odk_submission_id'],
                    'Yes' if r['reminder_eligible'] else 'No', r['reminder_reason'],
                    r['link_name'], r['short_url'], r['original_url']
                ])

            # Auto adjust column widths
            for col in ws.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = openpyxl.utils.get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 50)

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            response = HttpResponse(
                output.read(),
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = f'attachment; filename="campaign_{campaign.id}_link_tracking.xlsx"'
            return response

        # Pagination: 200 | 400 | 600 | 1000 | All
        page_size_str = request.GET.get('page_size', '200').strip().lower()
        if page_size_str == 'all':
            page_size = max(total_count, 1)
        else:
            try:
                page_size = int(page_size_str)
                if page_size not in (200, 400, 600, 1000):
                    page_size = 200
            except ValueError:
                page_size = 200

        try:
            page = max(1, int(request.GET.get('page', 1)))
        except ValueError:
            page = 1

        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_rows = rows[start_idx:end_idx]

        return Response({
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'total_count': total_count,
            'page': page,
            'page_size': page_size if page_size_str != 'all' else 'all',
            'total_pages': total_pages,
            'recipients': paginated_rows
        })


class CampaignReportLinkClicksView(APIView):
    """
    Recipient Click Activity: per-click events for recipient email links.
    Unions legacy root-level LinkClickEvent rows with RECIPIENT-type /c/
    visits, newest first. Shareable (anonymous) visits are excluded here;
    they belong to the shareable-links report. IP addresses are stored but
    excluded from this API (privacy; staff can use Django admin).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        from apps.tracking.models import (
            ShortenedLink, LinkClickEvent,
            CampaignTrackingLink, CampaignLinkClickEvent,
        )
        from apps.tracking.utils import build_campaign_short_url

        try:
            limit = int(request.GET.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        link_id = (request.GET.get('link_id') or '').strip()
        selected_sl_id = int(link_id) if link_id.isdigit() else None

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
        for e in legacy_qs.order_by('-clicked_at', '-id')[:limit]:
            sl = e.recipient_link.shortened_link
            name, email = contact_display(e.contact)
            rows.append({
                '_sort': (e.clicked_at, 'L', e.id),
                'clicked_at': e.clicked_at.isoformat(),
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
        for e in new_qs.order_by('-clicked_at', '-id')[:limit]:
            sl = e.link.shortened_link
            name, email = contact_display(e.contact)
            rows.append({
                '_sort': (e.clicked_at, 'C', e.id),
                'clicked_at': e.clicked_at.isoformat(),
                'contact_name': name,
                'contact_email': email,
                'contact_phone': e.contact.phone_number if e.contact else '-',
                'tracking_token': e.link.tracking_token,
                'link_name': (sl.link_name if sl else None) or e.link.name or 'Tracked Link',
                # Derived from the current base: the stored short_url
                # may have been minted under another environment.
                'short_url': build_campaign_short_url(
                    e.link.tracking_token),
                'destination_url': (e.link.destination_url or (sl.original_url if sl else '') or ''),
                'click_type': click_type_label(e.click_type),
                'browser': e.browser or 'Unknown',
                'operating_system': e.operating_system or 'Unknown',
                'device_type': e.device_type or 'Unknown',
                'referrer': e.referrer or '',
            })
        rows.sort(key=lambda r: r['_sort'], reverse=True)
        events = []
        for r in rows[:limit]:
            del r['_sort']
            events.append(r)

        links = [
            {'id': sl.id, 'name': sl.link_name or sl.original_url[:40]}
            for sl in ShortenedLink.objects.filter(
                campaign=campaign).order_by('link_name', 'id')
        ]

        return Response({
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'total_events': legacy_qs.count() + new_qs.count(),
            'links': links,
            'events': events,
        })


class CampaignReportShareableLinksView(APIView):
    """
    Shareable / Anonymous Link Activity Report.
    Covers SHAREABLE-type CampaignTrackingLink rows (/c/<token>/) and
    their CampaignLinkClickEvent visits. Fully separate from
    recipient-level email tracking: anonymous visits carry no contact
    attribution, so no "unique clicks" metric is offered (no visitor
    identity is tracked). RECIPIENT-type /c/ links are excluded here;
    they appear in the recipient Link Clicks report instead.
    Visitor IP addresses are stored but intentionally excluded from this
    API (privacy; available to staff via Django admin).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        from apps.tracking.models import CampaignTrackingLink, CampaignLinkClickEvent
        from apps.tracking.utils import build_campaign_short_url

        links = list(campaign.tracking_links.filter(
            link_type=CampaignTrackingLink.LinkType.SHAREABLE
        ).order_by('-created_at'))

        link_id = (request.GET.get('link_id') or '').strip()
        selected_link = None
        if link_id.isdigit():
            selected_link = next((l for l in links if l.id == int(link_id)), None)

        try:
            limit = int(request.GET.get('limit', 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        events_qs = CampaignLinkClickEvent.objects.filter(
            campaign=campaign,
            link__link_type=CampaignTrackingLink.LinkType.SHAREABLE,
        )
        if selected_link:
            events_qs = events_qs.filter(link=selected_link)
        events = list(events_qs.select_related('link').order_by('-clicked_at', '-id')[:limit])

        links_payload = []
        for l in links:
            links_payload.append({
                'id': l.id,
                'name': l.name or 'Untitled link',
                # Derived from the current base (see link-clicks).
                'short_url': build_campaign_short_url(l.tracking_token),
                'destination_url': l.resolve_destination(),
                'uses_campaign_default': not (l.destination_url or '').strip(),
                'is_active': l.is_active,
                'click_count': l.click_count,
                'human_click_count': l.human_click_count,
                'bot_click_count': l.bot_click_count,
                'first_clicked_at': l.first_clicked_at.isoformat() if l.first_clicked_at else None,
                'last_clicked_at': l.last_clicked_at.isoformat() if l.last_clicked_at else None,
            })

        def click_type_label(ct):
            return {
                'HUMAN': 'Human',
                'SUSPECTED_BOT': 'Suspected Bot',
                'UNKNOWN': 'Unknown',
            }.get(ct, 'Unknown')

        events_payload = [{
            'clicked_at': e.clicked_at.isoformat() if e.clicked_at else None,
            'link_id': e.link_id,
            'link_name': ((e.link.name if e.link else '') or 'Untitled link'),
            'click_type': click_type_label(e.click_type),
            'browser': e.browser or 'Other',
            'operating_system': e.operating_system or 'Other',
            'device_type': e.device_type or 'Unknown',
            'referrer': e.referrer or '',
        } for e in events]

        return Response({
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'campaign_destination_url': campaign.destination_url or '',
            'totals': {
                'links': len(links),
                'active_links': sum(1 for l in links if l.is_active),
                'total_clicks': sum(l.click_count for l in links),
                'human_clicks': sum(l.human_click_count for l in links),
                'bot_clicks': sum(l.bot_click_count for l in links),
            },
            'links': links_payload,
            'recent_events': events_payload,
            'recent_limit': limit,
        })


class CampaignExportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk, fmt):
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        fmt_lower = fmt.lower()
        if fmt_lower == 'xlsx':
            return export_campaign_xlsx(campaign)
        elif fmt_lower == 'csv':
            return export_campaign_csv(campaign)
        elif fmt_lower == 'pdf':
            return export_campaign_pdf(campaign)
        return Response({'error': f'Unsupported export format {fmt}'}, status=status.HTTP_400_BAD_REQUEST)


class DashboardStatsView(APIView):
    """Global system dashboard metrics (Section 77 & 78)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        total_contacts = Contact.objects.count()
        used_contacts = Contact.objects.filter(status=Contact.UsageStatus.USED).count()
        unused_contacts = Contact.objects.filter(status=Contact.UsageStatus.UNUSED).count()
        total_groups = ContactGroup.objects.count()
        total_campaigns = Campaign.objects.count()
        active_reminders = ReminderConfiguration.objects.filter(enabled=True, campaign__status=Campaign.Status.ACTIVE).count()

        emails_sent = CampaignMessage.objects.filter(sent_at__isnull=False).count()
        emails_delivered = CampaignMessage.objects.filter(delivered_at__isnull=False).count()
        emails_opened = CampaignMessage.objects.filter(opened_at__isnull=False).count()
        emails_clicked = CampaignMessage.objects.filter(clicked_at__isnull=False).count()
        unsubscribed = Contact.objects.filter(unsubscribed=True).count()
        bounced = Contact.objects.filter(email_status__in=[Contact.EmailStatus.HARD_BOUNCE, Contact.EmailStatus.SOFT_BOUNCE]).count()

        # Active campaigns summary table (Section 78)
        active_campaigns = []
        for c in Campaign.objects.filter(status__in=[Campaign.Status.ACTIVE, Campaign.Status.SCHEDULED])[:10]:
            groups = c.groups.all()
            contacts = Contact.objects.filter(groups__in=groups).distinct()
            recipients = contacts.count()
            c_used = contacts.filter(status=Contact.UsageStatus.USED).count()
            c_unused = contacts.filter(status=Contact.UsageStatus.UNUSED).count()
            comp_rate = round((c_used / recipients * 100), 1) if recipients > 0 else 0.0

            next_rem = None
            if hasattr(c, 'reminder_config') and c.reminder_config.enabled:
                next_rem = c.reminder_config.next_run_at

            active_campaigns.append({
                'id': c.id,
                'name': c.name,
                'recipients': recipients,
                'used': c_used,
                'unused': c_unused,
                'completion_rate': comp_rate,
                'next_reminder': next_rem,
                'status': c.status
            })

        return Response({
            'total_contacts': total_contacts,
            'used_contacts': used_contacts,
            'unused_contacts': unused_contacts,
            'completion_rate': round(used_contacts / total_contacts * 100, 1) if total_contacts > 0 else 0.0,
            'total_groups': total_groups,
            'total_campaigns': total_campaigns,
            'active_reminder_campaigns': active_reminders,
            'emails_sent': emails_sent,
            'delivered': emails_delivered,
            'opened': emails_opened,
            'clicked': emails_clicked,
            'unsubscribed': unsubscribed,
            'bounced': bounced,
            'active_campaigns': active_campaigns
        })
