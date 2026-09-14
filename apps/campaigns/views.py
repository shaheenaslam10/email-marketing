import re
import uuid
import logging
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Campaign, CampaignMessage
from .serializers import CampaignSerializer, CampaignMessageSerializer
from apps.contacts.models import Contact
from apps.senders.models import Sender
from apps.groups.models import TestEmailGroup, ContactGroup
from apps.email_providers.providers import get_email_provider
from .services import render_content_variables, validate_campaign_variables, wrap_tracking
from apps.reminders.services import launch_initial_campaign

logger = logging.getLogger(__name__)


class CampaignViewSet(viewsets.ModelViewSet):
    queryset = Campaign.objects.all().order_by('-created_at')
    serializer_class = CampaignSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def preview(self, request, pk=None):
        """Preview email as a specific contact (Section 37)."""
        campaign = self.get_object()
        contact_id = request.data.get('contact_id')
        contact = None
        if contact_id:
            contact = Contact.objects.filter(id=contact_id).first()
        if not contact:
            # Pick first contact from campaign groups or any contact
            contact = Contact.objects.filter(groups__in=campaign.groups.all()).first() or Contact.objects.first()

        if not contact:
            return Response({'error': 'No contact available for preview'}, status=status.HTTP_404_NOT_FOUND)

        req_subject = request.data.get('subject') if request.data.get('subject') is not None else campaign.subject
        req_html = request.data.get('html_content') if request.data.get('html_content') is not None else campaign.html_content

        subject = render_content_variables(req_subject, contact)
        html_content = render_content_variables(req_html, contact)
        # Resolve tracked links with the same generation logic as dispatch
        # so the preview shows real branded /c/ recipient URLs instead of
        # a literal {unique_link} placeholder (browser URL serialization
        # would otherwise display it as %7Bunique_link%7D).
        html_content = wrap_tracking(
            html_content=html_content,
            token_str=str(uuid.uuid4()),
            track_opens=campaign.track_opens,
            track_clicks=campaign.track_clicks,
            campaign=campaign,
            contact=contact,
        )

        return Response({
            'contact': {
                'id': contact.id,
                'name': contact.name,
                'email': contact.email,
                'phone': contact.phone_number,
                'job_id': contact.job_id,
                'status': contact.status,
            },
            'subject': subject,
            'html_content': html_content,
        })

    @action(detail=False, methods=['post'], url_path='render-preview')
    def render_preview(self, request):
        """Live preview endpoint for wizard, rendering subject & html with selected contact."""
        contact_id = request.data.get('contact_id')
        group_ids = request.data.get('group_ids', [])
        contact = None

        if contact_id:
            contact = Contact.objects.filter(id=contact_id).first()
        if not contact and group_ids:
            contact = Contact.objects.filter(groups__id__in=group_ids).first()
        if not contact:
            contact = Contact.objects.first()

        if not contact:
            return Response({'error': 'No contact available for preview'}, status=status.HTTP_404_NOT_FOUND)

        subject = request.data.get('subject', '')
        html_content = request.data.get('html_content', '')

        # Optional campaign context (wizard passes campaign_id when editing
        # a saved campaign) so preview links use the same branded /c/
        # recipient URLs as dispatch. Without a campaign the same
        # wrap_tracking() fallback as standalone test emails applies.
        campaign = None
        campaign_id = request.data.get('campaign_id') or request.data.get('campaign_pk')
        if campaign_id:
            try:
                campaign = Campaign.objects.filter(pk=int(campaign_id)).first()
            except (TypeError, ValueError):
                campaign = None

        rendered_subject = render_content_variables(subject, contact)
        rendered_html = render_content_variables(html_content, contact)
        rendered_html = wrap_tracking(
            html_content=rendered_html,
            token_str=str(uuid.uuid4()),
            track_opens=campaign.track_opens if campaign else True,
            track_clicks=campaign.track_clicks if campaign else True,
            campaign=campaign,
            contact=contact,
        )

        return Response({
            'contact': {
                'id': contact.id,
                'name': contact.name,
                'email': contact.email,
                'phone': contact.phone_number,
                'job_id': contact.job_id,
                'status': contact.status,
            },
            'subject': rendered_subject,
            'html_content': rendered_html,
        })

    @action(detail=True, methods=['post'])
    def test_email(self, request, pk=None):
        """Sends test email(s) for an existing campaign (Section 38)."""
        return self._handle_test_email(request, pk=pk)

    @action(detail=False, methods=['post'], url_path='test_email')
    def test_email_standalone(self, request):
        """Sends test email(s) with custom or editor content without requiring saved campaign."""
        return self._handle_test_email(request, pk=None)

    def _handle_test_email(self, request, pk=None):
        """
        Processes test email dispatch supporting:
        - Single email address
        - Comma, semicolon, or newline-separated multiple email addresses
        - Predefined Testing Groups (TestEmailGroup) or Contact Groups
        - Live overrides for subject, html_content, sender, and sample contact
        """
        campaign = None
        if pk:
            campaign = Campaign.objects.filter(pk=pk).first()
        elif request.data.get('campaign_id'):
            campaign = Campaign.objects.filter(pk=request.data.get('campaign_id')).first()

        # Gather recipient email addresses
        recipients = []

        # 1. Direct email field (single or comma/semicolon/newline-delimited)
        raw_email = request.data.get('email', '')
        if raw_email and isinstance(raw_email, str):
            for e in re.split(r'[,;\s\n\r\t]+', raw_email):
                clean_e = e.strip()
                if clean_e and '@' in clean_e and clean_e.lower() not in [x.lower() for x in recipients]:
                    recipients.append(clean_e)

        # 2. Direct emails array
        raw_emails = request.data.get('emails', [])
        if isinstance(raw_emails, list):
            for e in raw_emails:
                if isinstance(e, str):
                    for sub in re.split(r'[,;\s\n\r\t]+', e):
                        clean_sub = sub.strip()
                        if clean_sub and '@' in clean_sub and clean_sub.lower() not in [x.lower() for x in recipients]:
                            recipients.append(clean_sub)

        # 3. Predefined Testing Group (TestEmailGroup or ContactGroup)
        test_group_id = request.data.get('test_group_id')
        if test_group_id:
            # Check TestEmailGroup first
            t_grp = TestEmailGroup.objects.filter(id=test_group_id).first()
            if t_grp:
                for e in t_grp.email_list:
                    if e.lower() not in [x.lower() for x in recipients]:
                        recipients.append(e)
            else:
                # Fallback to ContactGroup
                c_grp = ContactGroup.objects.filter(id=test_group_id).first()
                if c_grp:
                    for c in c_grp.contacts.all():
                        if c.email and c.email.lower() not in [x.lower() for x in recipients]:
                            recipients.append(c.email)

        if not recipients:
            return Response({
                'error': 'Please provide at least one valid recipient email or select a testing group.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Subject and HTML content (request data overrides campaign content if provided)
        req_subject = request.data.get('subject')
        req_html = request.data.get('html_content')

        base_subject = req_subject if (req_subject is not None and str(req_subject).strip()) else (campaign.subject if campaign else "Test Campaign Preview")

        # Validate that req_html is meaningful content and not dummy placeholder or empty Quill block
        is_valid_html = (
            req_html is not None
            and str(req_html).strip()
            and str(req_html).strip() not in ('<p><br></p>', '<p></p>', '')
            and str(req_html).strip() != '<p>Test Email Body</p>'
        )
        base_html = req_html if is_valid_html else (campaign.html_content if campaign and campaign.html_content else "<p>This is a test email preview.</p>")

        # Sender account
        sender_id = request.data.get('sender_id') or request.data.get('sender')
        sender = None
        if sender_id:
            sender = Sender.objects.filter(id=sender_id).first()
        if not sender and campaign and campaign.sender:
            sender = campaign.sender
        if not sender:
            sender = Sender.objects.filter(is_active=True).first() or Sender.objects.first()

        sender_name = sender.name if sender else 'System'
        sender_email = sender.email if sender else 'test@platform.local'
        reply_to = sender.reply_to if sender and sender.reply_to else None
        provider_type = sender.get_provider_type_display() if sender else 'Sandbox Simulator'

        # Sample contact for variable interpolation
        contact_id = request.data.get('contact_id')
        group_ids = request.data.get('group_ids', [])
        contact = None
        if contact_id:
            contact = Contact.objects.filter(id=contact_id).first()
        if not contact and campaign:
            contact = Contact.objects.filter(groups__in=campaign.groups.all()).first()
        if not contact and group_ids:
            contact = Contact.objects.filter(groups__id__in=group_ids).first()
        if not contact:
            contact = Contact.objects.first()

        provider = get_email_provider(sender)

        results = []
        success_count = 0
        last_provider_msg_id = None

        for to_addr in recipients:
            # Render subject & HTML with sample contact data
            if contact:
                # Interpolate sample contact data but customize recipient email
                rendered_subj = f"[TEST] {render_content_variables(base_subject, contact)}"
                rendered_html = render_content_variables(base_html, contact)
            else:
                rendered_subj = f"[TEST] {base_subject}"
                rendered_html = base_html

            # Resolve tracked links exactly like production dispatch so test
            # emails carry working URLs (recipient-specific short URLs when a
            # campaign + sample contact exist, legacy redirect fallback
            # otherwise) instead of a literal {unique_link} placeholder.
            final_test_html = wrap_tracking(
                html_content=rendered_html,
                token_str=str(uuid.uuid4()),
                track_opens=campaign.track_opens if campaign else True,
                track_clicks=campaign.track_clicks if campaign else True,
                campaign=campaign,
                contact=contact,
            )

            res = provider.send_email(
                to_email=to_addr,
                subject=rendered_subj,
                html_content=final_test_html,
                text_content="",
                reply_to=reply_to,
                log_context={
                    'campaign_id': campaign.id if campaign else None,
                    'source': 'test-email',
                },
            )

            # Record in SandboxEmail for instant in-app webmail inspection
            try:
                from apps.sandbox.models import SandboxEmail
                SandboxEmail.objects.create(
                    to_email=to_addr,
                    from_name=sender_name,
                    from_email=sender_email,
                    reply_to=reply_to or '',
                    subject=rendered_subj,
                    html_content=final_test_html,
                    text_content="",
                    provider_type=sender.provider_type if sender else 'SANDBOX'
                )
            except Exception:
                pass

            if res.success:
                success_count += 1
                last_provider_msg_id = res.provider_message_id or 'queued'
                results.append({
                    'email': to_addr,
                    'status': 'sent',
                    'provider_message_id': res.provider_message_id or 'queued',
                })
            else:
                results.append({
                    'email': to_addr,
                    'status': 'failed',
                    'error': res.error_message or 'Unknown provider error',
                })

        advisory_msg = (
            "Emails accepted by mail relay. If not visible in your inbox within 1-2 minutes, check your Spam/Junk folder "
            "(domain SPF verification recommended for external deliverability)."
        )

        resp_status = status.HTTP_200_OK if success_count > 0 else status.HTTP_400_BAD_REQUEST
        return Response({
            'status': 'success' if success_count == len(recipients) else ('partial' if success_count > 0 else 'failed'),
            'total': len(recipients),
            'sent_count': success_count,
            'failed_count': len(recipients) - success_count,
            'recipients': results,
            'message': f"Test email dispatched to {success_count} of {len(recipients)} recipient(s).",
            'provider': provider_type,
            'sender': f'{sender_name} <{sender_email}>',
            'provider_message_id': last_provider_msg_id or 'smtp-queued',
            'advisory': advisory_msg,
        }, status=resp_status)

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Starts immediate, scheduled, or batched sending of the campaign, or saves as draft."""
        campaign = self.get_object()

        send_mode = (request.data.get('send_mode') or 'now').lower() if request.data else 'now'
        scheduled_at_str = request.data.get('scheduled_at') if request.data else None
        batch_size = request.data.get('batch_size') if request.data else None
        batch_interval = request.data.get('batch_interval_minutes') if request.data else None

        # 1. Draft Mode
        if send_mode == 'draft':
            campaign.status = Campaign.Status.DRAFT
            campaign.send_mode = Campaign.SendMode.DRAFT
            campaign.save(update_fields=['status', 'send_mode'])
            return Response({
                'status': 'success',
                'message': f'Campaign "{campaign.name}" saved as draft.',
                'campaign_status': campaign.status
            })

        # 2. Scheduled Mode
        if send_mode in ['schedule', 'scheduled'] and scheduled_at_str:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(str(scheduled_at_str))
            if not dt:
                return Response({'error': 'Invalid scheduled date/time format'}, status=status.HTTP_400_BAD_REQUEST)
            campaign.status = Campaign.Status.SCHEDULED
            campaign.send_mode = Campaign.SendMode.SCHEDULED
            campaign.scheduled_at = dt
            campaign.save(update_fields=['status', 'send_mode', 'scheduled_at'])
            return Response({
                'status': 'success',
                'message': f'Campaign "{campaign.name}" scheduled for {dt.strftime("%b %d, %Y %I:%M %p")}.',
                'campaign_status': campaign.status,
                'scheduled_at': dt.isoformat()
            })

        # 3. Batches Mode
        if send_mode in ['batches', 'batch']:
            b_size = int(batch_size or campaign.batch_size or 50)
            b_interval = int(batch_interval or campaign.batch_interval_minutes or 30)
            campaign.status = Campaign.Status.ACTIVE
            campaign.send_mode = Campaign.SendMode.BATCHES
            campaign.batch_size = b_size
            campaign.batch_interval_minutes = b_interval
            campaign.save(update_fields=['status', 'send_mode', 'batch_size', 'batch_interval_minutes'])

            dispatched = launch_initial_campaign(campaign, batch_size=b_size, force_resend=True)
            return Response({
                'status': 'success',
                'message': f'Campaign launched in batches of {b_size} every {b_interval}m. {dispatched} email(s) dispatched in first batch.',
                'campaign_status': campaign.status,
                'dispatched_count': dispatched,
                'batch_size': b_size,
                'batch_interval_minutes': b_interval
            })

        # 4. Immediate Send (Send Now)
        campaign.status = Campaign.Status.ACTIVE
        campaign.send_mode = Campaign.SendMode.IMMEDIATE
        campaign.save(update_fields=['status', 'send_mode'])
        dispatched = launch_initial_campaign(campaign, force_resend=True)
        return Response({
            'status': 'success',
            'message': f'Campaign dispatched successfully. {dispatched} emails dispatched.',
            'campaign_status': campaign.status,
            'dispatched_count': dispatched
        })

    @action(detail=True, methods=['post'], url_path='send-reminder')
    def send_reminder(self, request, pk=None):
        """
        Triggers a reminder cycle: Send now, Schedule for later, or Send in batches.
        """
        campaign = self.get_object()
        from apps.reminders.services import execute_reminder_cycle

        send_mode = (request.data.get('send_mode') or 'now').lower() if request.data else 'now'
        scheduled_at_str = request.data.get('scheduled_at') if request.data else None
        batch_size = request.data.get('batch_size') if request.data else None
        batch_interval = request.data.get('batch_interval_minutes') if request.data else None
        custom_subject = request.data.get('subject') if request.data else None

        # Schedule reminder for later
        if send_mode in ['schedule', 'scheduled'] and scheduled_at_str:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(str(scheduled_at_str))
            if not dt:
                return Response({'error': 'Invalid scheduled date/time format'}, status=status.HTTP_400_BAD_REQUEST)
            if hasattr(campaign, 'reminder_config'):
                rem_cfg = campaign.reminder_config
                rem_cfg.next_run_at = dt
                if custom_subject:
                    rem_cfg.custom_subject = custom_subject
                rem_cfg.enabled = True
                rem_cfg.save(update_fields=['next_run_at', 'enabled'] + (['custom_subject'] if custom_subject else []))
            return Response({
                'status': 'success',
                'message': f'Reminder for "{campaign.name}" scheduled for {dt.strftime("%b %d, %Y %I:%M %p")}.',
                'scheduled_at': dt.isoformat()
            })

        odk_info = None
        if campaign.odk_dataset:
            odk_info = f"ODK Entity List '{campaign.odk_dataset.name}'"
        elif campaign.odk_form:
            odk_info = f"ODK Form '{campaign.odk_form.name}'"

        try:
            b_size = int(batch_size) if batch_size and send_mode in ['batches', 'batch'] else None
            b_interval = int(batch_interval) if batch_interval and send_mode in ['batches', 'batch'] else None

            # Persist batch settings if provided
            update_fields = []
            if b_size:
                campaign.batch_size = b_size
                update_fields.append('batch_size')
            if b_interval:
                campaign.batch_interval_minutes = b_interval
                update_fields.append('batch_interval_minutes')
            if send_mode in ['batches', 'batch']:
                campaign.send_mode = 'BATCHES'
                update_fields.append('send_mode')
            if update_fields:
                campaign.save(update_fields=update_fields)

            cycle = execute_reminder_cycle(campaign, manual_trigger=True, custom_subject=custom_subject, batch_size=b_size)
            skipped_count = max(0, cycle.eligible_count - cycle.sent_count)

            msg = f"Reminder cycle #{cycle.cycle_number} dispatched! Checked {odk_info or 'ODK entity list'}. Sent to {cycle.sent_count} unused contact(s). {skipped_count} contact(s) marked USED were excluded."
            if b_size and cycle.eligible_count > b_size:
                msg = f"Reminder cycle #{cycle.cycle_number} started in batches! Dispatched first batch of {cycle.sent_count} emails to unused contacts. {skipped_count} contact(s) marked USED were excluded."

            return Response({
                'status': 'success',
                'message': msg,
                'cycle_number': cycle.cycle_number,
                'sent_count': cycle.sent_count,
                'eligible_count': cycle.eligible_count,
                'odk_synced_info': odk_info,
            })
        except Exception as e:
            return Response({'status': 'failed', 'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='reminders/send-now')
    def reminders_send_now(self, request, pk=None):
        """Alias for send_reminder action."""
        return self.send_reminder(request, pk=pk)

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        campaign = self.get_object()
        campaign.status = Campaign.Status.PAUSED
        campaign.save(update_fields=['status'])
        return Response({'status': 'paused', 'message': 'Campaign and automated reminders paused.'})

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        campaign = self.get_object()
        campaign.status = Campaign.Status.ACTIVE
        campaign.save(update_fields=['status'])
        return Response({'status': 'active', 'message': 'Campaign resumed.'})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        campaign = self.get_object()
        campaign.status = Campaign.Status.CANCELLED
        campaign.save(update_fields=['status'])
        return Response({'status': 'cancelled', 'message': 'Campaign cancelled.'})

    @action(detail=True, methods=['get', 'post'], url_path='tracking-links')
    def tracking_links(self, request, pk=None):
        """
        Campaign-level shareable tracking links (anonymous, for external
        distribution). GET lists, POST generates a new link.
        Only SHAREABLE links are listed here; per-recipient /c/ email
        links are managed automatically and stay out of this UI.
        """
        from apps.tracking.models import CampaignTrackingLink
        from apps.tracking.serializers import CampaignTrackingLinkSerializer
        from apps.tracking.utils import (
            generate_unique_tracking_token, build_campaign_short_url,
        )
        campaign = self.get_object()

        if request.method == 'GET':
            links = campaign.tracking_links.filter(
                link_type=CampaignTrackingLink.LinkType.SHAREABLE
            ).order_by('-created_at')
            return Response({
                'campaign_id': campaign.id,
                'destination_url': campaign.destination_url or '',
                'links': CampaignTrackingLinkSerializer(links, many=True).data,
            })

        serializer = CampaignTrackingLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = generate_unique_tracking_token(CampaignTrackingLink, length=8)
        link = CampaignTrackingLink.objects.create(
            campaign=campaign,
            name=(serializer.validated_data.get('name') or '').strip(),
            destination_url=(serializer.validated_data.get('destination_url') or '').strip(),
            is_active=serializer.validated_data.get('is_active', True),
            tracking_token=token,
            short_url=build_campaign_short_url(token, request),
        )
        return Response(
            CampaignTrackingLinkSerializer(link).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True, methods=['get', 'put', 'patch', 'delete'],
        url_path=r'tracking-links/(?P<link_id>[^/.]+)',
    )
    def tracking_link_detail(self, request, pk=None, link_id=None):
        """Retrieve, update (label/destination/active flag) or delete one link."""
        from apps.tracking.models import CampaignTrackingLink
        from apps.tracking.serializers import CampaignTrackingLinkSerializer
        campaign = self.get_object()
        link = CampaignTrackingLink.objects.filter(
            pk=link_id, campaign=campaign,
            link_type=CampaignTrackingLink.LinkType.SHAREABLE,
        ).first()
        if not link:
            return Response(
                {'error': 'Tracking link not found for this campaign.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == 'DELETE':
            link.delete()
            return Response(
                {'status': 'deleted', 'message': 'Tracking link deleted.'},
                status=status.HTTP_200_OK,
            )

        partial = request.method == 'PATCH'
        serializer = CampaignTrackingLinkSerializer(
            link, data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CampaignTrackingLinkSerializer(link).data)

    @action(
        detail=True, methods=['post'],
        url_path=r'tracking-links/(?P<link_id>[^/.]+)/regenerate',
    )
    def tracking_link_regenerate(self, request, pk=None, link_id=None):
        """Issues a fresh token/URL for a link (invalidates the old URL)."""
        from apps.tracking.models import CampaignTrackingLink
        from apps.tracking.serializers import CampaignTrackingLinkSerializer
        from apps.tracking.utils import (
            generate_unique_tracking_token, build_campaign_short_url,
        )
        campaign = self.get_object()
        link = CampaignTrackingLink.objects.filter(
            pk=link_id, campaign=campaign,
            link_type=CampaignTrackingLink.LinkType.SHAREABLE,
        ).first()
        if not link:
            return Response(
                {'error': 'Tracking link not found for this campaign.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        token = generate_unique_tracking_token(CampaignTrackingLink, length=8)
        link.tracking_token = token
        link.short_url = build_campaign_short_url(token, request)
        link.save(update_fields=['tracking_token', 'short_url', 'updated_at'])
        return Response(CampaignTrackingLinkSerializer(link).data)

    @action(detail=True, methods=['get'])
    def validate_vars(self, request, pk=None):
        campaign = self.get_object()
        contacts = list(Contact.objects.filter(groups__in=campaign.groups.all()).distinct())
        diagnostics = validate_campaign_variables(campaign.html_content, campaign.subject, contacts)
        return Response(diagnostics)

    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        campaign = self.get_object()
        qs = campaign.messages.all().order_by('-queued_at')
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = CampaignMessageSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = CampaignMessageSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': 'No campaign IDs provided'}, status=status.HTTP_400_BAD_REQUEST)
        count, _ = Campaign.objects.filter(id__in=ids).delete()
        return Response({'status': 'success', 'message': f'{count} campaign(s) deleted successfully.'})

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        orig = self.get_object()
        from apps.reminders.models import ReminderConfiguration

        base_name = orig.name
        new_name = f"{base_name} (Copy)"
        counter = 2
        while Campaign.objects.filter(name=new_name).exists():
            new_name = f"{base_name} (Copy {counter})"
            counter += 1

        new_campaign = Campaign.objects.create(
            name=new_name,
            campaign_type=orig.campaign_type,
            description=orig.description,
            status=Campaign.Status.DRAFT,
            sender=orig.sender,
            odk_form=orig.odk_form,
            odk_dataset=orig.odk_dataset,
            completion_status_source=orig.completion_status_source,
            initial_recipient_rule=orig.initial_recipient_rule,
            subject=orig.subject,
            preview_text=orig.preview_text,
            html_content=orig.html_content,
            text_content=orig.text_content,
            track_opens=orig.track_opens,
            track_clicks=orig.track_clicks,
            destination_url=orig.destination_url,
            created_by=request.user if request.user.is_authenticated else orig.created_by,
        )
        new_campaign.groups.set(orig.groups.all())

        if hasattr(orig, 'reminder_config') and orig.reminder_config:
            rc = orig.reminder_config
            ReminderConfiguration.objects.create(
                campaign=new_campaign,
                enabled=rc.enabled,
                interval_value=rc.interval_value,
                interval_unit=rc.interval_unit,
                duration_value=rc.duration_value,
                duration_unit=rc.duration_unit,
                max_reminders=rc.max_reminders,
                sync_odk_before_send=rc.sync_odk_before_send,
                stop_when_used=rc.stop_when_used,
                reminder_template=rc.reminder_template,
                custom_subject=rc.custom_subject,
                custom_html_content=rc.custom_html_content,
            )

        serializer = CampaignSerializer(new_campaign)
        return Response({
            'status': 'success',
            'message': f"Campaign '{new_name}' duplicated successfully as Draft!",
            'campaign': serializer.data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='bulk-duplicate')
    def bulk_duplicate(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': 'No campaign IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.reminders.models import ReminderConfiguration
        created_campaigns = []

        for cid in ids:
            orig = Campaign.objects.filter(id=cid).first()
            if not orig:
                continue

            base_name = orig.name
            new_name = f"{base_name} (Copy)"
            counter = 2
            while Campaign.objects.filter(name=new_name).exists():
                new_name = f"{base_name} (Copy {counter})"
                counter += 1

            new_c = Campaign.objects.create(
                name=new_name,
                campaign_type=orig.campaign_type,
                description=orig.description,
                status=Campaign.Status.DRAFT,
                sender=orig.sender,
                odk_form=orig.odk_form,
                odk_dataset=orig.odk_dataset,
                completion_status_source=orig.completion_status_source,
                initial_recipient_rule=orig.initial_recipient_rule,
                subject=orig.subject,
                preview_text=orig.preview_text,
                html_content=orig.html_content,
                text_content=orig.text_content,
                track_opens=orig.track_opens,
                track_clicks=orig.track_clicks,
                destination_url=orig.destination_url,
                created_by=request.user if request.user.is_authenticated else orig.created_by,
            )
            new_c.groups.set(orig.groups.all())

            if hasattr(orig, 'reminder_config') and orig.reminder_config:
                rc = orig.reminder_config
                ReminderConfiguration.objects.create(
                    campaign=new_c,
                    enabled=rc.enabled,
                    interval_value=rc.interval_value,
                    interval_unit=rc.interval_unit,
                    duration_value=rc.duration_value,
                    duration_unit=rc.duration_unit,
                    max_reminders=rc.max_reminders,
                    sync_odk_before_send=rc.sync_odk_before_send,
                    stop_when_used=rc.stop_when_used,
                    reminder_template=rc.reminder_template,
                    custom_subject=rc.custom_subject,
                    custom_html_content=rc.custom_html_content,
                )
            created_campaigns.append(new_c.name)

        return Response({
            'status': 'success',
            'message': f"{len(created_campaigns)} campaign(s) duplicated successfully as Draft!",
            'created': created_campaigns
        }, status=status.HTTP_201_CREATED)
