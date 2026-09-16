import io
import csv
from datetime import datetime
from django.http import HttpResponse
from django.utils import timezone
from django.db.models import Q
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas

from .services import (
    build_report_filename,
    get_campaign_full_report,
    get_recipient_lifecycle_rows,
    reminder_status_label,
)
from apps.campaigns.models import Campaign, CampaignMessage
from apps.contacts.models import Contact


def _format_datetime(val) -> str:
    """Formats datetime string or object to human-friendly local time (e.g. 'Sep 10, 2026, 10:47 AM')."""
    if not val:
        return "-"
    if isinstance(val, str):
        try:
            from dateutil.parser import parse
            val = parse(val)
        except Exception:
            try:
                from django.utils.dateparse import parse_datetime
                val = parse_datetime(val)
            except Exception:
                return str(val)
    if hasattr(val, 'astimezone'):
        try:
            val = val.astimezone()
            return val.strftime('%b %d, %Y, %I:%M %p')
        except Exception:
            return val.strftime('%b %d, %Y, %I:%M %p')
    elif hasattr(val, 'strftime'):
        return val.strftime('%b %d, %Y, %I:%M %p')
    return str(val)


def _get_campaign_metadata(campaign: Campaign):
    """Extracts UI header card details matching the web analytics view."""
    subject = campaign.subject or "-"
    sender_display = getattr(campaign.sender, 'display_from', f"{campaign.sender.name} <{campaign.sender.email}>") if campaign.sender else "-"
    campaign_type = campaign.get_campaign_type_display() if hasattr(campaign, 'get_campaign_type_display') else campaign.campaign_type

    odk_name = "-"
    if campaign.odk_dataset:
        odk_name = campaign.odk_dataset.name
    elif campaign.odk_form:
        odk_name = campaign.odk_form.name

    has_reminders = hasattr(campaign, 'reminder_config') and campaign.reminder_config.enabled
    next_rem_dt = campaign.reminder_config.next_run_at if has_reminders else None

    if has_reminders and next_rem_dt:
        automation_status = "Active (Scheduled)"
        next_reminder_str = _format_datetime(next_rem_dt)
    elif has_reminders:
        automation_status = "Active"
        next_reminder_str = "None Scheduled"
    else:
        automation_status = "Inactive"
        next_reminder_str = "None"

    return {
        'name': campaign.name,
        'status': campaign.status,
        'subject': subject,
        'sender': sender_display,
        'type': campaign_type,
        'odk_name': odk_name,
        'automation_status': automation_status,
        'next_reminder': next_reminder_str,
    }


def _get_recipients_log(campaign: Campaign):
    """Retrieves full campaign recipients log matching Deliverability drilldown in Screenshot 1."""
    messages = campaign.messages.select_related('contact').all().order_by('-sent_at', '-id')
    results = []
    for m in messages:
        c = m.contact
        stage_name = 'Initial Email' if m.message_type == CampaignMessage.MessageType.INITIAL else f'Reminder {m.reminder_sequence}'
        results.append({
            'name': c.name if c else (m.to_email or 'Unknown Contact'),
            'email': m.to_email or (c.email if c else ''),
            'job_id': c.job_id if c and c.job_id else '-',
            'stage': stage_name,
            'sent_at': _format_datetime(m.sent_at or m.delivered_at),
            'delivery_status': m.status or 'SENT',
            'survey_status': (c.status if c else 'UNKNOWN') or 'UNKNOWN',
            'details': m.error_message or m.skip_reason or '-',
        })
    return results


# -------------------------------------------------------------------------
# EXCEL EXPORT (Multi-Tab matching Screenshot 1, 2, 3, 4)
# -------------------------------------------------------------------------

def export_campaign_xlsx(campaign: Campaign) -> HttpResponse:
    """
    Generates a high-fidelity, beautifully styled multi-tab Excel workbook
    faithfully reflecting the Campaign Analytics web UI and user screenshots.
    """
    report = get_campaign_full_report(campaign)
    meta = _get_campaign_metadata(campaign)
    kpi = report['kpi']
    conv = report['survey_conversion']
    recipients = _get_recipients_log(campaign)

    wb = openpyxl.Workbook()

    # Color Palette & Styles
    font_family = "Segoe UI"
    thin_border_color = "CBD5E1"
    thin_side = Side(style='thin', color=thin_border_color)
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    # Styles
    navy_header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    navy_header_font = Font(name=font_family, size=10, bold=True, color="FFFFFF")

    slate_header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    slate_header_font = Font(name=font_family, size=10, bold=True, color="FFFFFF")

    sub_header_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    sub_header_font = Font(name=font_family, size=10, bold=True, color="334155")

    meta_label_font = Font(name=font_family, size=9, bold=True, color="64748B")
    meta_val_font = Font(name=font_family, size=10, bold=True, color="0F172A")

    title_font = Font(name=font_family, size=14, bold=True, color="FFFFFF")
    section_title_font = Font(name=font_family, size=11, bold=True, color="0F172A")
    section_sub_font = Font(name=font_family, size=9, italic=True, color="64748B")

    regular_font = Font(name=font_family, size=9, color="1E293B")
    bold_regular_font = Font(name=font_family, size=9, bold=True, color="1E293B")

    # Status fills
    green_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    green_font = Font(name=font_family, size=9, bold=True, color="166534")

    amber_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    amber_font = Font(name=font_family, size=9, bold=True, color="92400E")

    blue_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    blue_font = Font(name=font_family, size=9, bold=True, color="1E40AF")

    rose_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    rose_font = Font(name=font_family, size=9, bold=True, color="991B1B")

    stripe_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    # -------------------------------------------------------------
    # Helper to format metadata card at top of sheets
    # -------------------------------------------------------------
    def write_header_card(ws, sheet_title):
        ws.append([f"Campaign Analytics: {meta['name']} ({meta['status']})"])
        ws.merge_cells("A1:G1")
        c1 = ws["A1"]
        c1.fill = navy_header_fill
        c1.font = title_font
        c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[1].height = 28

        ws.append(["SUBJECT LINE", meta['subject'], "", "AUTOMATION", meta['automation_status']])
        ws.append(["SENDER", meta['sender'], "", "NEXT REMINDER", meta['next_reminder']])
        ws.append(["TYPE", meta['type'], "", "ODK ENTITY LIST", meta['odk_name']])

        for r in range(2, 5):
            ws.row_dimensions[r].height = 18
            # Col A
            ws.cell(row=r, column=1).font = meta_label_font
            ws.cell(row=r, column=2).font = meta_val_font
            # Col D
            ws.cell(row=r, column=4).font = meta_label_font
            ws.cell(row=r, column=5).font = meta_val_font
            for c in range(1, 8):
                ws.cell(row=r, column=c).fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

        ws.append([])  # Spacer row

    # =============================================================
    # SHEET 1: Overview & Deliverability (Screenshot 1 & KPI cards)
    # =============================================================
    ws1 = wb.active
    ws1.title = "Overview & Deliverability"
    write_header_card(ws1, "Overview & Deliverability")

    # KPI Summary Cards Block
    curr_row = 6
    ws1.cell(row=curr_row, column=1, value="KEY PERFORMANCE INDICATORS").font = section_title_font
    curr_row += 1

    kpi_headers = ["USED (Survey Completed)", "UNUSED (Survey Pending)", "COMPLETION RATE", "DELIVERED", "DELIVERY RATE", "UNIQUE OPENS", "UNIQUE CLICKS"]
    kpi_vals = [
        f"{conv['used_count']:,}",
        f"{conv['unused_count']:,}",
        f"{conv['completion_rate']}%",
        f"{kpi['delivered_count']:,}",
        f"{kpi['delivery_rate']}%",
        f"{kpi['unique_opened_count']:,}",
        f"{kpi['unique_clicks_count']:,}",
    ]
    for c_idx, (h, v) in enumerate(zip(kpi_headers, kpi_vals), start=1):
        ws1.cell(row=curr_row, column=c_idx, value=h).font = sub_header_font
        ws1.cell(row=curr_row, column=c_idx).fill = sub_header_fill
        ws1.cell(row=curr_row, column=c_idx).alignment = Alignment(horizontal="center", vertical="center")
        ws1.cell(row=curr_row, column=c_idx).border = cell_border

        v_cell = ws1.cell(row=curr_row + 1, column=c_idx, value=v)
        v_cell.border = cell_border
        v_cell.alignment = Alignment(horizontal="center", vertical="center")
        if c_idx == 1:
            v_cell.fill = green_fill
            v_cell.font = Font(name=font_family, size=12, bold=True, color="166534")
        elif c_idx == 2:
            v_cell.fill = amber_fill
            v_cell.font = Font(name=font_family, size=12, bold=True, color="92400E")
        elif c_idx == 3:
            v_cell.fill = blue_fill
            v_cell.font = Font(name=font_family, size=12, bold=True, color="1E40AF")
        else:
            v_cell.font = Font(name=font_family, size=12, bold=True, color="0F172A")

    ws1.row_dimensions[curr_row].height = 20
    ws1.row_dimensions[curr_row + 1].height = 24
    curr_row += 3

    # Section: Email Deliverability Metrics Table (Screenshot 1 top table)
    ws1.cell(row=curr_row, column=1, value="Email Deliverability Metrics").font = section_title_font
    curr_row += 1
    ws1.cell(row=curr_row, column=1, value="Dispatch, delivery, and bounce rates for this campaign.").font = section_sub_font
    curr_row += 1

    deliv_cols = ["DELIVERY STATUS", "COUNT", "RATE", "RECIPIENTS LIST"]
    for c_idx, title in enumerate(deliv_cols, start=1):
        cell = ws1.cell(row=curr_row, column=c_idx, value=title)
        cell.fill = slate_header_fill
        cell.font = slate_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="right" if c_idx in (2, 3) else ("center" if c_idx == 4 else "left"), vertical="center")
    ws1.row_dimensions[curr_row].height = 22
    curr_row += 1

    for d in report['deliverability_table']:
        r_cells = [
            ws1.cell(row=curr_row, column=1, value=d['status']),
            ws1.cell(row=curr_row, column=2, value=d['count']),
            ws1.cell(row=curr_row, column=3, value=f"{d['rate']}%"),
            ws1.cell(row=curr_row, column=4, value="View Contacts" if d['count'] > 0 else "-"),
        ]
        r_cells[0].font = bold_regular_font
        r_cells[1].font = bold_regular_font
        r_cells[1].alignment = Alignment(horizontal="right")
        r_cells[2].font = bold_regular_font
        r_cells[2].alignment = Alignment(horizontal="right")
        r_cells[3].font = regular_font
        r_cells[3].alignment = Alignment(horizontal="center")

        # Color indicator
        if d['status'] == 'Sent':
            r_cells[0].font = Font(name=font_family, size=9, bold=True, color="2563EB")
        elif d['status'] == 'Delivered':
            r_cells[0].font = Font(name=font_family, size=9, bold=True, color="059669")
        elif d['status'] in ('Soft Bounce', 'Hard Bounce'):
            r_cells[0].font = Font(name=font_family, size=9, bold=True, color="D97706")
        elif d['status'] == 'Failed':
            r_cells[0].font = Font(name=font_family, size=9, bold=True, color="DC2626")

        for c in r_cells:
            c.border = cell_border
        ws1.row_dimensions[curr_row].height = 20
        curr_row += 1

    curr_row += 2

    # Section: Survey Conversion After Email Stage (Screenshot 2)
    ws1.cell(row=curr_row, column=1, value="Survey Conversion After Email Stage (Section 65)").font = section_title_font
    curr_row += 1
    ws1.cell(row=curr_row, column=1, value="Tracks approximate conversion stage when contact status flipped from UNUSED to USED, along with reminder send dates and upcoming schedules.").font = section_sub_font
    curr_row += 1

    stage_cols = ["STAGE", "DISPATCH / SCHEDULED DATE", "BECAME USED"]
    for c_idx, title in enumerate(stage_cols, start=1):
        cell = ws1.cell(row=curr_row, column=c_idx, value=title)
        cell.fill = slate_header_fill
        cell.font = slate_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="right" if c_idx == 3 else "left", vertical="center")
    ws1.row_dimensions[curr_row].height = 22
    curr_row += 1

    for s in conv['stage_breakdown']:
        if s.get('is_sent') and s.get('sent_at'):
            dispatch_text = f"Sent on {_format_datetime(s['sent_at'])}"
        elif s.get('scheduled_at'):
            dispatch_text = f"Scheduled for {_format_datetime(s['scheduled_at'])}"
        else:
            dispatch_text = "Pending previous stages"

        c1 = ws1.cell(row=curr_row, column=1, value=f"Converted after {s['stage']}")
        c2 = ws1.cell(row=curr_row, column=2, value=dispatch_text)
        c3 = ws1.cell(row=curr_row, column=3, value=s.get('became_used', 0))

        c1.font = bold_regular_font
        c2.font = regular_font
        c3.font = Font(name=font_family, size=9, bold=True, color="059669")
        c3.alignment = Alignment(horizontal="right")

        for cell in (c1, c2, c3):
            cell.border = cell_border
        ws1.row_dimensions[curr_row].height = 20
        curr_row += 1

    curr_row += 2

    # Section: Automated Reminder Cycle Performance (Screenshot 3)
    ws1.cell(row=curr_row, column=1, value="Automated Reminder Cycle Performance (Section 66)").font = section_title_font
    curr_row += 1
    ws1.cell(row=curr_row, column=1, value="Performance metrics and dispatch / schedule timestamps for each reminder cycle.").font = section_sub_font
    curr_row += 1

    funnel_cols = ["STAGE & DISPATCH TIME", "ELIGIBLE", "SENT", "DELIVERED", "OPENED", "BECAME USED", "STILL UNUSED"]
    for c_idx, title in enumerate(funnel_cols, start=1):
        cell = ws1.cell(row=curr_row, column=c_idx, value=title)
        cell.fill = slate_header_fill
        cell.font = slate_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="right" if c_idx > 1 else "left", vertical="center")
    ws1.row_dimensions[curr_row].height = 22
    curr_row += 1

    for r in report['reminder_stages']:
        if r.get('is_sent') and r.get('sent_at'):
            d_info = f"Sent on {_format_datetime(r['sent_at'])}"
        elif r.get('scheduled_at'):
            d_info = f"Scheduled for {_format_datetime(r['scheduled_at'])}"
        else:
            d_info = "Pending dispatch"

        stage_full = f"{r['stage']} ({d_info})"
        is_upcoming = r.get('is_upcoming', False)

        row_vals = [
            stage_full,
            r['eligible'],
            "-" if is_upcoming else r['sent'],
            "-" if is_upcoming else r['delivered'],
            "-" if is_upcoming else r['opened'],
            r['became_used'],
            r['still_unused'],
        ]

        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws1.cell(row=curr_row, column=c_idx, value=val)
            cell.border = cell_border
            if c_idx == 1:
                cell.font = bold_regular_font
                cell.alignment = Alignment(horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="right")
                if c_idx == 6:
                    cell.font = Font(name=font_family, size=9, bold=True, color="059669")
                elif c_idx == 7:
                    cell.font = Font(name=font_family, size=9, bold=True, color="D97706")
                else:
                    cell.font = regular_font

        ws1.row_dimensions[curr_row].height = 20
        curr_row += 1

    # =============================================================
    # SHEET 2: Recipients List (Screenshot 1 bottom table)
    # =============================================================
    ws2 = wb.create_sheet(title="Recipients List")
    write_header_card(ws2, "Recipients List")

    curr_row2 = 6
    ws2.cell(row=curr_row2, column=1, value=f"Recipients: All ({len(recipients)} contacts)").font = section_title_font
    curr_row2 += 1
    ws2.cell(row=curr_row2, column=1, value="Detailed list of email recipients and status logs for this campaign.").font = section_sub_font
    curr_row2 += 1

    recip_headers = [
        "CONTACT NAME", "EMAIL ADDRESS", "JOB ID", "STAGE",
        "SENT / RECORDED AT", "DELIVERY STATUS", "SURVEY STATUS", "DETAILS / ERROR"
    ]
    for c_idx, title in enumerate(recip_headers, start=1):
        cell = ws2.cell(row=curr_row2, column=c_idx, value=title)
        cell.fill = navy_header_fill
        cell.font = navy_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="center" if c_idx in (6, 7) else "left", vertical="center")
    ws2.row_dimensions[curr_row2].height = 24
    curr_row2 += 1

    for idx, r in enumerate(recipients):
        row_cells = [
            ws2.cell(row=curr_row2, column=1, value=r['name']),
            ws2.cell(row=curr_row2, column=2, value=r['email']),
            ws2.cell(row=curr_row2, column=3, value=r['job_id']),
            ws2.cell(row=curr_row2, column=4, value=r['stage']),
            ws2.cell(row=curr_row2, column=5, value=r['sent_at']),
            ws2.cell(row=curr_row2, column=6, value=r['delivery_status']),
            ws2.cell(row=curr_row2, column=7, value=r['survey_status']),
            ws2.cell(row=curr_row2, column=8, value=r['details']),
        ]

        # Base styling
        for c in row_cells:
            c.border = cell_border
            c.font = regular_font
            if idx % 2 == 1:
                c.fill = stripe_fill

        row_cells[0].font = bold_regular_font
        row_cells[2].font = Font(name="Consolas", size=9, color="334155")
        row_cells[5].alignment = Alignment(horizontal="center")
        row_cells[6].alignment = Alignment(horizontal="center")

        # Status badge fills
        deliv_stat = (r['delivery_status'] or '').upper()
        if deliv_stat in ('DELIVERED', 'SENT'):
            row_cells[5].fill = green_fill if deliv_stat == 'DELIVERED' else blue_fill
            row_cells[5].font = green_font if deliv_stat == 'DELIVERED' else blue_font
        elif 'BOUNCE' in deliv_stat:
            row_cells[5].fill = amber_fill
            row_cells[5].font = amber_font
        elif deliv_stat == 'FAILED':
            row_cells[5].fill = rose_fill
            row_cells[5].font = rose_font

        surv_stat = (r['survey_status'] or '').upper()
        if surv_stat == 'USED':
            row_cells[6].fill = green_fill
            row_cells[6].font = green_font
        elif surv_stat == 'UNUSED':
            row_cells[6].fill = amber_fill
            row_cells[6].font = amber_font

        ws2.row_dimensions[curr_row2].height = 20
        curr_row2 += 1

    # =============================================================
    # SHEET 3: Survey Conversions (Screenshot 2)
    # =============================================================
    ws3 = wb.create_sheet(title="Survey Conversions")
    write_header_card(ws3, "Survey Conversions")

    curr_row3 = 6
    ws3.cell(row=curr_row3, column=1, value="Survey Conversion After Email Stage (Section 65)").font = section_title_font
    curr_row3 += 1
    ws3.cell(row=curr_row3, column=1, value="Tracks approximate conversion stage when contact status flipped from UNUSED to USED, along with reminder send dates and upcoming schedules.").font = section_sub_font
    curr_row3 += 1

    for c_idx, title in enumerate(stage_cols, start=1):
        cell = ws3.cell(row=curr_row3, column=c_idx, value=title)
        cell.fill = navy_header_fill
        cell.font = navy_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="right" if c_idx == 3 else "left", vertical="center")
    ws3.row_dimensions[curr_row3].height = 24
    curr_row3 += 1

    for s in conv['stage_breakdown']:
        if s.get('is_sent') and s.get('sent_at'):
            dispatch_text = f"Sent on {_format_datetime(s['sent_at'])}"
        elif s.get('scheduled_at'):
            dispatch_text = f"Scheduled for {_format_datetime(s['scheduled_at'])}"
        else:
            dispatch_text = "Pending previous stages"

        c1 = ws3.cell(row=curr_row3, column=1, value=f"Converted after {s['stage']}")
        c2 = ws3.cell(row=curr_row3, column=2, value=dispatch_text)
        c3 = ws3.cell(row=curr_row3, column=3, value=s.get('became_used', 0))

        c1.font = bold_regular_font
        c2.font = regular_font
        c3.font = Font(name=font_family, size=10, bold=True, color="059669")
        c3.alignment = Alignment(horizontal="right")

        for cell in (c1, c2, c3):
            cell.border = cell_border
        ws3.row_dimensions[curr_row3].height = 22
        curr_row3 += 1

    # =============================================================
    # SHEET 4: Reminder Funnel (Screenshot 3)
    # =============================================================
    ws4 = wb.create_sheet(title="Reminder Funnel")
    write_header_card(ws4, "Reminder Funnel")

    curr_row4 = 6
    ws4.cell(row=curr_row4, column=1, value="Automated Reminder Cycle Performance (Section 66)").font = section_title_font
    curr_row4 += 1
    ws4.cell(row=curr_row4, column=1, value="Performance metrics and dispatch / schedule timestamps for each reminder cycle.").font = section_sub_font
    curr_row4 += 1

    for c_idx, title in enumerate(funnel_cols, start=1):
        cell = ws4.cell(row=curr_row4, column=c_idx, value=title)
        cell.fill = navy_header_fill
        cell.font = navy_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="right" if c_idx > 1 else "left", vertical="center")
    ws4.row_dimensions[curr_row4].height = 24
    curr_row4 += 1

    for r in report['reminder_stages']:
        if r.get('is_sent') and r.get('sent_at'):
            d_info = f"Sent on {_format_datetime(r['sent_at'])}"
        elif r.get('scheduled_at'):
            d_info = f"Scheduled for {_format_datetime(r['scheduled_at'])}"
        else:
            d_info = "Pending dispatch"

        stage_full = f"{r['stage']} ({d_info})"
        is_upcoming = r.get('is_upcoming', False)

        row_vals = [
            stage_full,
            r['eligible'],
            "-" if is_upcoming else r['sent'],
            "-" if is_upcoming else r['delivered'],
            "-" if is_upcoming else r['opened'],
            r['became_used'],
            r['still_unused'],
        ]

        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws4.cell(row=curr_row4, column=c_idx, value=val)
            cell.border = cell_border
            if c_idx == 1:
                cell.font = bold_regular_font
                cell.alignment = Alignment(horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="right")
                if c_idx == 6:
                    cell.font = Font(name=font_family, size=9, bold=True, color="059669")
                elif c_idx == 7:
                    cell.font = Font(name=font_family, size=9, bold=True, color="D97706")
                else:
                    cell.font = regular_font

        ws4.row_dimensions[curr_row4].height = 22
        curr_row4 += 1

    # =============================================================
    # SHEET 5: Still Unused Respondents (Screenshot 4)
    # =============================================================
    ws5 = wb.create_sheet(title="Still Unused Respondents")
    write_header_card(ws5, "Still Unused Respondents")

    curr_row5 = 6
    ws5.cell(row=curr_row5, column=1, value="Still Unused Respondents").font = section_title_font
    curr_row5 += 1
    ws5.cell(row=curr_row5, column=1, value="Contacts who have not yet submitted their ODK Central survey.").font = section_sub_font
    curr_row5 += 1

    unused_headers = ["NAME", "EMAIL", "JOBID", "EMAILS SENT", "REMINDERS", "OPENED"]
    for c_idx, title in enumerate(unused_headers, start=1):
        cell = ws5.cell(row=curr_row5, column=c_idx, value=title)
        cell.fill = navy_header_fill
        cell.font = navy_header_font
        cell.border = cell_border
        cell.alignment = Alignment(horizontal="center" if c_idx in (4, 5, 6) else "left", vertical="center")
    ws5.row_dimensions[curr_row5].height = 24
    curr_row5 += 1

    still_unused = report.get('still_unused_contacts', [])
    if not still_unused:
        empty_cell = ws5.cell(row=curr_row5, column=1, value="All respondents have completed the survey (100% conversion)!")
        empty_cell.font = section_sub_font
        ws5.merge_cells(start_row=curr_row5, start_column=1, end_row=curr_row5, end_column=6)
    else:
        for idx, c in enumerate(still_unused):
            row_cells = [
                ws5.cell(row=curr_row5, column=1, value=c['name']),
                ws5.cell(row=curr_row5, column=2, value=c['email']),
                ws5.cell(row=curr_row5, column=3, value=c['job_id'] or '-'),
                ws5.cell(row=curr_row5, column=4, value=c['emails_sent']),
                ws5.cell(row=curr_row5, column=5, value=c['reminders_count']),
                ws5.cell(row=curr_row5, column=6, value="Yes" if c['opened'] else "No"),
            ]

            for cell in row_cells:
                cell.border = cell_border
                cell.font = regular_font
                if idx % 2 == 1:
                    cell.fill = stripe_fill

            row_cells[0].font = bold_regular_font
            row_cells[2].font = Font(name="Consolas", size=9, color="334155")
            row_cells[3].alignment = Alignment(horizontal="center")
            row_cells[4].alignment = Alignment(horizontal="center")
            row_cells[4].font = Font(name=font_family, size=9, bold=True, color="4F46E5")
            row_cells[5].alignment = Alignment(horizontal="center")
            if c['opened']:
                row_cells[5].font = Font(name=font_family, size=9, bold=True, color="059669")

            ws5.row_dimensions[curr_row5].height = 20
            curr_row5 += 1

    # -------------------------------------------------------------
    # Auto-adjust column widths & enable grid lines across all sheets
    # -------------------------------------------------------------
    for ws in [ws1, ws2, ws3, ws4, ws5]:
        ws.views.sheetView[0].showGridLines = True
        for col in ws.columns:
            max_len = 0
            for cell in col:
                val = cell.value
                if val is not None:
                    s_val = str(val)
                    # Don't let merged banner in row 1 dominate column A width
                    if cell.row == 1 and cell.column == 1:
                        continue
                    max_len = max(max_len, len(s_val))
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"campaign_{campaign.id}_analytics_report.xlsx"
    response = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# -------------------------------------------------------------------------
# PDF EXPORT (Landscape, Multi-Section matching Screenshots 1, 2, 3, 4)
# -------------------------------------------------------------------------

class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas that adds running headers, running footers,
    and total page counts (e.g. 'Page 1 of 3') dynamically.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))

        # Footer divider line
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.75)
        self.line(36, 26, 756, 26)

        # Footer text
        self.drawString(36, 16, "Email & Survey Automation Platform  •  Confidential Campaign Analytics Report")
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(756, 16, page_str)

        # Header for page 2+
        if self._pageNumber > 1:
            self.drawString(36, 582, "Campaign Analytics Report")
            self.setStrokeColor(colors.HexColor("#E2E8F0"))
            self.setLineWidth(0.5)
            self.line(36, 576, 756, 576)

        self.restoreState()


def export_campaign_pdf(campaign: Campaign) -> HttpResponse:
    """
    Generates a high-quality landscape PDF analytics report matching
    the web application dashboard and user screenshots.
    """
    report = get_campaign_full_report(campaign)
    meta = _get_campaign_metadata(campaign)
    kpi = report['kpi']
    conv = report['survey_conversion']
    recipients = _get_recipients_log(campaign)

    buf = io.BytesIO()
    # Usable width: 792 - 72 = 720 points
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(letter),
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Custom styles
    h1_style = ParagraphStyle(
        'HeaderTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#0F172A')
    )

    section_title = ParagraphStyle(
        'SecTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor('#0F172A'),
        spaceAfter=2
    )

    section_sub = ParagraphStyle(
        'SecSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#64748B'),
        spaceAfter=6
    )

    cell_bold = ParagraphStyle(
        'CellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#0F172A')
    )

    cell_normal = ParagraphStyle(
        'CellNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#334155')
    )

    cell_code = ParagraphStyle(
        'CellCode',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#1E293B')
    )

    cell_center = ParagraphStyle(
        'CellCenter',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.HexColor('#0F172A')
    )

    cell_right = ParagraphStyle(
        'CellRight',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        alignment=2,
        textColor=colors.HexColor('#0F172A')
    )

    cell_emerald = ParagraphStyle(
        'CellEmerald',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        alignment=2,
        textColor=colors.HexColor('#059669')
    )

    cell_amber = ParagraphStyle(
        'CellAmber',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        alignment=2,
        textColor=colors.HexColor('#D97706')
    )

    story = []

    # -------------------------------------------------------------
    # 1. Header Card Box (Card container)
    # -------------------------------------------------------------
    hdr_left = f"""
    <font size="14" color="#0F172A"><b>Campaign Analytics: {meta['name']}</b></font><br/><br/>
    <font size="8" color="#64748B"><b>SUBJECT LINE:</b></font> <font size="8" color="#0F172A"><b>{meta['subject']}</b></font><br/>
    <font size="8" color="#64748B"><b>Sender:</b></font> <font size="8" color="#334155">{meta['sender']}</font> &nbsp;&nbsp;|&nbsp;&nbsp;
    <font size="8" color="#64748B"><b>Type:</b></font> <font size="8" color="#334155">{meta['type']}</font><br/>
    <font size="8" color="#64748B"><b>ODK Entity List:</b></font> <font size="8" color="#059669"><b>{meta['odk_name']}</b></font>
    """

    status_color = "#059669" if meta['status'] == "ACTIVE" else "#2563EB"
    hdr_right = f"""
    <div align="right">
      <font size="9" color="{status_color}"><b>STATUS: {meta['status']}</b></font><br/><br/>
      <font size="8" color="#64748B"><b>AUTOMATION:</b></font> <font size="8" color="#0F172A"><b>{meta['automation_status']}</b></font><br/>
      <font size="8" color="#64748B"><b>NEXT REMINDER:</b></font> <font size="8" color="#4338CA"><b>{meta['next_reminder']}</b></font>
    </div>
    """

    header_table_data = [[Paragraph(hdr_left, styles['Normal']), Paragraph(hdr_right, styles['Normal'])]]
    header_table = Table(header_table_data, colWidths=[480, 240])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F8FAFC')),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#CBD5E1')),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))

    # -------------------------------------------------------------
    # 2. Key Performance Indicators (Prominent Cards)
    # -------------------------------------------------------------
    kpi_card_data = [
        [
            Paragraph('<font size="7" color="#065F46"><b>USED (SURVEY COMPLETED)</b></font><br/><font size="16" color="#065F46"><b>' + f"{conv['used_count']:,}" + '</b></font><br/><font size="6.5" color="#047857">Submissions synced via ODK</font>', styles['Normal']),
            Paragraph('<font size="7" color="#92400E"><b>UNUSED (SURVEY PENDING)</b></font><br/><font size="16" color="#92400E"><b>' + f"{conv['unused_count']:,}" + '</b></font><br/><font size="6.5" color="#B45309">Eligible for automated reminders</font>', styles['Normal']),
            Paragraph('<font size="7" color="#1E40AF"><b>COMPLETION RATE</b></font><br/><font size="16" color="#1E40AF"><b>' + f"{conv['completion_rate']}%" + '</b></font><br/><font size="6.5" color="#1D4ED8">Used / Total Recipients</font>', styles['Normal']),
            Paragraph('<font size="7" color="#1E293B"><b>DELIVERED EMAILS</b></font><br/><font size="16" color="#0F172A"><b>' + f"{kpi['delivered_count']:,}" + '</b></font><br/><font size="6.5" color="#059669"><b>' + f"{kpi['delivery_rate']}%" + ' delivery rate</b></font>', styles['Normal']),
        ]
    ]
    kpi_table = Table(kpi_card_data, colWidths=[180, 180, 180, 180])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#DCFCE7')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#FEF3C7')),
        ('BACKGROUND', (2, 0), (2, 0), colors.HexColor('#DBEAFE')),
        ('BACKGROUND', (3, 0), (3, 0), colors.HexColor('#F1F5F9')),
        ('BOX', (0, 0), (0, 0), 0.5, colors.HexColor('#86EFAC')),
        ('BOX', (1, 0), (1, 0), 0.5, colors.HexColor('#FCD34D')),
        ('BOX', (2, 0), (2, 0), 0.5, colors.HexColor('#93C5FD')),
        ('BOX', (3, 0), (3, 0), 0.5, colors.HexColor('#CBD5E1')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 12))

    # -------------------------------------------------------------
    # 3. Deliverability Metrics Table (Screenshot 1 top table)
    # -------------------------------------------------------------
    story.append(Paragraph("Email Deliverability Metrics", section_title))
    story.append(Paragraph("Summary of dispatch, delivery, and bounce rates for this campaign.", section_sub))

    deliv_rows = [
        [
            Paragraph("<b>DELIVERY STATUS</b>", cell_bold),
            Paragraph("<b>COUNT</b>", cell_right),
            Paragraph("<b>RATE</b>", cell_right),
            Paragraph("<b>RECIPIENTS LIST</b>", cell_center),
        ]
    ]

    for d in report['deliverability_table']:
        # Dot bullet color
        color_dot = "#2563EB" if d['status'] == "Sent" else ("#059669" if d['status'] == "Delivered" else ("#DC2626" if d['status'] == "Failed" else "#D97706"))
        status_para = Paragraph(f'<font color="{color_dot}">●</font> <b>{d["status"]}</b>', cell_normal)
        count_para = Paragraph(f"{d['count']:,}", cell_right)
        rate_para = Paragraph(f"{d['rate']}%", cell_right)
        btn_para = Paragraph("View Contacts" if d['count'] > 0 else "-", cell_center)
        deliv_rows.append([status_para, count_para, rate_para, btn_para])

    deliv_t = Table(deliv_rows, colWidths=[240, 160, 160, 160])
    deliv_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    # Header cells text color white
    for c in deliv_rows[0]:
        c.style.textColor = colors.whitesmoke
    story.append(deliv_t)
    story.append(Spacer(1, 14))

    # -------------------------------------------------------------
    # 4. Survey Conversion After Email Stage (Screenshot 2)
    # -------------------------------------------------------------
    story.append(Paragraph("Survey Conversion After Email Stage (Section 65)", section_title))
    story.append(Paragraph("Tracks approximate conversion stage when contact status flipped from UNUSED to USED, along with reminder send dates and upcoming schedules.", section_sub))

    stage_rows = [
        [
            Paragraph("<b>STAGE</b>", cell_bold),
            Paragraph("<b>DISPATCH / SCHEDULED DATE</b>", cell_bold),
            Paragraph("<b>BECAME USED</b>", cell_right),
        ]
    ]

    for s in conv['stage_breakdown']:
        if s.get('is_sent') and s.get('sent_at'):
            date_html = f'<font color="#059669"><b>✓ Sent on {_format_datetime(s["sent_at"])}</b></font>'
        elif s.get('scheduled_at'):
            date_html = f'<font color="#4F46E5"><b>🕒 Scheduled for {_format_datetime(s["scheduled_at"])}</b></font>'
        else:
            date_html = '<font color="#94A3B8">Pending previous stages</font>'

        stage_rows.append([
            Paragraph(f"Converted after {s['stage']}", cell_bold),
            Paragraph(date_html, cell_normal),
            Paragraph(str(s.get('became_used', 0)), cell_emerald),
        ])

    stage_t = Table(stage_rows, colWidths=[260, 300, 160])
    stage_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E293B')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
    ]))
    for c in stage_rows[0]:
        c.style.textColor = colors.whitesmoke
    story.append(stage_t)
    story.append(Spacer(1, 14))

    # -------------------------------------------------------------
    # 5. Automated Reminder Cycle Performance (Screenshot 3)
    # -------------------------------------------------------------
    story.append(Paragraph("Automated Reminder Cycle Performance (Section 66)", section_title))
    story.append(Paragraph("Performance metrics and dispatch / schedule timestamps for each reminder cycle.", section_sub))

    funnel_rows = [
        [
            Paragraph("<b>STAGE & DISPATCH TIME</b>", cell_bold),
            Paragraph("<b>ELIGIBLE</b>", cell_right),
            Paragraph("<b>SENT</b>", cell_right),
            Paragraph("<b>DELIVERED</b>", cell_right),
            Paragraph("<b>OPENED</b>", cell_right),
            Paragraph("<b>BECAME USED</b>", cell_emerald),
            Paragraph("<b>STILL UNUSED</b>", cell_amber),
        ]
    ]

    for r in report['reminder_stages']:
        if r.get('is_sent') and r.get('sent_at'):
            d_line = f'<br/><font size="7" color="#059669">✓ Sent on {_format_datetime(r["sent_at"])}</font>'
        elif r.get('scheduled_at'):
            d_line = f'<br/><font size="7" color="#4F46E5">🕒 Scheduled for {_format_datetime(r["scheduled_at"])}</font>'
        else:
            d_line = '<br/><font size="7" color="#94A3B8">Pending dispatch</font>'

        is_up = r.get('is_upcoming', False)

        funnel_rows.append([
            Paragraph(f"<b>{r['stage']}</b>{d_line}", cell_normal),
            Paragraph(str(r['eligible']), cell_right),
            Paragraph("-" if is_up else str(r['sent']), cell_right),
            Paragraph("-" if is_up else str(r['delivered']), cell_right),
            Paragraph("-" if is_up else str(r['opened']), cell_right),
            Paragraph(str(r['became_used']), cell_emerald),
            Paragraph(str(r['still_unused']), cell_amber),
        ])

    funnel_t = Table(funnel_rows, colWidths=[210, 85, 85, 85, 85, 85, 85])
    funnel_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E293B')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    for c in funnel_rows[0]:
        c.style.textColor = colors.whitesmoke
    story.append(funnel_t)

    # -------------------------------------------------------------
    # 6. PageBreak -> Still Unused Respondents & Detailed Recipients
    # -------------------------------------------------------------
    story.append(PageBreak())

    # Still Unused Respondents Table (Screenshot 4)
    story.append(Paragraph(f"Still Unused Respondents ({conv['unused_count']:,} contacts)", section_title))
    story.append(Paragraph("Contacts who have not yet submitted their ODK Central survey.", section_sub))

    unused_rows = [
        [
            Paragraph("<b>NAME</b>", cell_bold),
            Paragraph("<b>EMAIL</b>", cell_bold),
            Paragraph("<b>JOBID</b>", cell_bold),
            Paragraph("<b>EMAILS SENT</b>", cell_center),
            Paragraph("<b>REMINDERS</b>", cell_center),
            Paragraph("<b>OPENED</b>", cell_center),
        ]
    ]

    still_unused = report.get('still_unused_contacts', [])
    if not still_unused:
        unused_rows.append([Paragraph("All respondents have completed the survey (100% conversion)!", cell_normal), "", "", "", "", ""])
    else:
        for c in still_unused:
            opened_txt = '<font color="#059669"><b>Yes</b></font>' if c['opened'] else '<font color="#94A3B8">No</font>'
            unused_rows.append([
                Paragraph(c['name'], cell_bold),
                Paragraph(c['email'], cell_normal),
                Paragraph(c['job_id'] or '-', cell_code),
                Paragraph(str(c['emails_sent']), cell_center),
                Paragraph(f'<font color="#4F46E5"><b>{c["reminders_count"]}</b></font>', cell_center),
                Paragraph(opened_txt, cell_center),
            ])

    unused_t = Table(unused_rows, colWidths=[150, 190, 190, 60, 65, 65])
    unused_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    for c in unused_rows[0]:
        c.style.textColor = colors.whitesmoke
    story.append(unused_t)
    story.append(Spacer(1, 16))

    # -------------------------------------------------------------
    # 7. Recipients & Delivery Status Log (Screenshot 1 bottom table)
    # -------------------------------------------------------------
    story.append(Paragraph(f"Recipients: Sent & Delivered Contact Log ({len(recipients):,} contacts)", section_title))
    story.append(Paragraph("Detailed list of email recipients, delivery statuses, and survey submission statuses.", section_sub))

    recip_table_rows = [
        [
            Paragraph("<b>CONTACT NAME</b>", cell_bold),
            Paragraph("<b>EMAIL ADDRESS</b>", cell_bold),
            Paragraph("<b>JOB ID</b>", cell_bold),
            Paragraph("<b>STAGE</b>", cell_normal),
            Paragraph("<b>SENT AT</b>", cell_normal),
            Paragraph("<b>DELIVERY</b>", cell_center),
            Paragraph("<b>SURVEY</b>", cell_center),
        ]
    ]

    for r in recipients:
        deliv_badge = f'<font color="#059669"><b>{r["delivery_status"]}</b></font>' if r['delivery_status'] == 'DELIVERED' else (
            f'<font color="#2563EB"><b>{r["delivery_status"]}</b></font>' if r['delivery_status'] == 'SENT' else f'<font color="#DC2626"><b>{r["delivery_status"]}</b></font>'
        )

        surv_badge = f'<font color="#059669"><b>{r["survey_status"]}</b></font>' if r['survey_status'] == 'USED' else f'<font color="#D97706"><b>{r["survey_status"]}</b></font>'

        recip_table_rows.append([
            Paragraph(r['name'], cell_bold),
            Paragraph(r['email'], cell_normal),
            Paragraph(r['job_id'], cell_code),
            Paragraph(r['stage'], cell_normal),
            Paragraph(r['sent_at'], cell_normal),
            Paragraph(deliv_badge, cell_center),
            Paragraph(surv_badge, cell_center),
        ])

    recip_t = Table(recip_table_rows, colWidths=[120, 170, 170, 80, 90, 45, 45])
    recip_t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    for c in recip_table_rows[0]:
        c.style.textColor = colors.whitesmoke
    story.append(recip_t)

    # Build Document with NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    buf.seek(0)

    filename = f"campaign_{campaign.id}_analytics_report.pdf"
    response = HttpResponse(buf.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# -------------------------------------------------------------------------
# CSV EXPORT
# -------------------------------------------------------------------------

def export_campaign_csv(campaign: Campaign, filters=None) -> HttpResponse:
    """Recipient-level operational CSV for a single campaign.

    Same source of truth as the workbook's Recipient Lifecycle sheet:
    shared get_recipient_lifecycle_rows, identical lifecycle values,
    and the shared reminder_status_label. One row per recipient.
    UTF-8 with BOM so Excel opens it correctly; QUOTE_MINIMAL keeps
    commas/newlines inside fields intact.
    """
    rows = get_recipient_lifecycle_rows(campaign, filters or {})
    # Workbook parity: missing dates render as an em dash everywhere.
    missing_as_dash = lambda value: '\u2014' if value == '-' else value
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL, lineterminator='\n')
    writer.writerow([
        'Campaign', 'Name', 'Phone', 'Email', 'Sent', 'Sent At',
        'Opened', 'Opened At', 'Clicked', 'Click Count',
        'First Clicked At', 'Tracking Token', 'Completed',
        'Completed At', 'ODK Submission ID', 'Reminder Eligible',
        'Reminder Status', 'Reminder Reason',
    ])
    for r in rows:
        writer.writerow([
            campaign.name,
            r['name'], r['phone'], r['email'],
            'YES' if r['has_sent'] else '\u2014', missing_as_dash(r['email_sent_at']),
            'YES' if r['has_opened'] else '\u2014', missing_as_dash(r['email_opened_at']),
            'YES' if r['has_clicked'] else '\u2014', r['total_clicks'],
            missing_as_dash(r['first_click']), r['tracking_token'] or '\u2014',
            'COMPLETED' if r['contact_status'] == 'USED' else 'INCOMPLETE',
            missing_as_dash(r['completed_at']), r['odk_submission_id'] or '\u2014',
            'ELIGIBLE' if r['reminder_eligible'] else 'SUPPRESSED',
            reminder_status_label(r), r['reminder_reason'],
        ])
    filename = build_report_filename(
        [campaign.name],
        lifecycle=str((filters or {}).get('lifecycle', 'all') or 'all'),
        search=str((filters or {}).get('search', '') or ''),
        ext='csv',
    )
    response = HttpResponse(
        '\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="%s"' % filename
    return response
