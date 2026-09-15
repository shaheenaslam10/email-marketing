import base64
from django.http import HttpResponse, HttpResponseRedirect
from django.views import View
from django.shortcuts import render
from django.utils import timezone
from .models import (
    TrackingToken, EmailEvent, UnsubscribeRecord, RecipientLink,
    ShortenedLink, LinkClickEvent, CampaignTrackingLink, CampaignLinkClickEvent,
)
from apps.campaigns.models import CampaignMessage
from apps.contacts.models import Contact

# 1x1 transparent GIF binary
TRANSPARENT_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class OpenTrackingView(View):
    """Handles 1x1 pixel requests for email open tracking (Section 75)."""

    def get(self, request, token):
        try:
            token_obj = TrackingToken.objects.select_related('message', 'message__contact').get(token=token)
            msg = token_obj.message
            now = timezone.now()

            if not msg.opened_at:
                msg.opened_at = now
            if msg.status in [CampaignMessage.Status.SENT, CampaignMessage.Status.DELIVERED]:
                msg.status = CampaignMessage.Status.OPENED
            msg.save(update_fields=['opened_at', 'status'])

            EmailEvent.objects.create(
                message=msg,
                event_type=EmailEvent.EventType.OPEN,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500]
            )
        except TrackingToken.DoesNotExist:
            pass

        response = HttpResponse(TRANSPARENT_GIF, content_type="image/gif")
        response['Cache-Control'] = "no-cache, no-store, must-revalidate, max-age=0"
        return response


class ClickTrackingView(View):
    """Tracks URL click and redirects user to target destination (Section 76)."""

    def get(self, request, token):
        from .utils import validate_destination_url
        raw_url = (request.GET.get('url') or '').strip()
        # SECURITY: never redirect to an unvalidated caller-supplied URL.
        # Blocks javascript:/data: XSS vectors, CRLF header injection and
        # other non-http(s) schemes. Missing param keeps legacy '/' fallback.
        if raw_url and not validate_destination_url(raw_url):
            return render(request, 'tracking/link_error.html', {
                'error_message': 'The destination URL is invalid or unsafe.'
            }, status=400)
        target_url = raw_url or '/'
        try:
            token_obj = TrackingToken.objects.select_related('message', 'message__contact').get(token=token)
            msg = token_obj.message
            now = timezone.now()

            if not msg.opened_at:
                msg.opened_at = now
            if not msg.clicked_at:
                msg.clicked_at = now
            msg.status = CampaignMessage.Status.CLICKED
            msg.save(update_fields=['opened_at', 'clicked_at', 'status'])

            EmailEvent.objects.create(
                message=msg,
                event_type=EmailEvent.EventType.CLICK,
                url_clicked=target_url,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500]
            )
        except TrackingToken.DoesNotExist:
            pass

        return HttpResponseRedirect(target_url)


class UnsubscribeView(View):
    """Processes contact unsubscribe and confirms suppression (Section 73)."""

    def get(self, request, token):
        try:
            token_obj = TrackingToken.objects.select_related('message', 'message__contact', 'message__campaign').get(token=token)
            msg = token_obj.message
            contact = msg.contact

            if not contact.unsubscribed:
                contact.unsubscribed = True
                contact.email_status = Contact.EmailStatus.UNSUBSCRIBED
                contact.save(update_fields=['unsubscribed', 'email_status'])

                UnsubscribeRecord.objects.create(
                    contact=contact,
                    campaign=msg.campaign,
                    reason="User clicked unsubscribe link"
                )

                EmailEvent.objects.create(
                    message=msg,
                    event_type=EmailEvent.EventType.UNSUBSCRIBE,
                    ip_address=get_client_ip(request),
                    user_agent=request.META.get('HTTP_USER_AGENT', '')[:500]
                )

            return render(request, 'tracking/unsubscribed.html', {
                'contact_email': contact.email,
                'campaign_name': msg.campaign.name
            })
        except TrackingToken.DoesNotExist:
            return render(request, 'tracking/unsubscribed.html', {
                'contact_email': 'Your email',
                'campaign_name': 'our mailing list'
            })


class ShortUrlRedirectView(View):
    """
    Public high-performance short URL redirect endpoint.
    GET /:tracking_token (e.g. GET <tracking-base>/A7K29X)
    (Requirement 2, 3, 4, 13, 14)
    """

    def get(self, request, token):
        token = (token or '').strip()
        recipient_link = RecipientLink.objects.select_related(
            'shortened_link', 'campaign', 'contact'
        ).filter(tracking_token=token).first()

        if not recipient_link:
            return render(request, 'tracking/link_error.html', {
                'error_message': 'The tracking link was not found or has expired.'
            }, status=404)

        shortened_link = recipient_link.shortened_link
        destination_url = (shortened_link.original_url or '').strip()

        from .utils import validate_destination_url, detect_bot_and_device
        if not validate_destination_url(destination_url):
            return render(request, 'tracking/link_error.html', {
                'error_message': 'The destination URL is invalid or unsafe.'
            }, status=400)

        # Detect bot / scanner / browser / device
        det = detect_bot_and_device(request, recipient_link)
        now = timezone.now()

        # Update recipient link counters
        if not recipient_link.first_clicked_at:
            recipient_link.first_clicked_at = now
        recipient_link.last_clicked_at = now
        recipient_link.click_count += 1
        if det['click_type'] == 'SUSPECTED_BOT':
            recipient_link.bot_click_count += 1
        else:
            recipient_link.human_click_count += 1

        recipient_link.save(update_fields=[
            'first_clicked_at', 'last_clicked_at', 'click_count',
            'human_click_count', 'bot_click_count'
        ])

        # Record granular LinkClickEvent
        LinkClickEvent.objects.create(
            recipient_link=recipient_link,
            campaign=recipient_link.campaign,
            contact=recipient_link.contact,
            ip_address=get_client_ip(request),
            user_agent=det['user_agent'],
            browser=det['browser'],
            operating_system=det['operating_system'],
            device_type=det['device_type'],
            referrer=det['referrer'],
            click_type=det['click_type'],
            metadata={'reason': det['detection_reason']}
        )

        # Update CampaignMessage and legacy EmailEvent if campaign and contact exist
        if recipient_link.campaign and recipient_link.contact:
            msg = CampaignMessage.objects.filter(
                campaign=recipient_link.campaign,
                contact=recipient_link.contact
            ).order_by('-sent_at', '-id').first()

            if msg:
                if det['click_type'] != 'SUSPECTED_BOT':
                    if not msg.clicked_at:
                        msg.clicked_at = now
                        msg.status = CampaignMessage.Status.CLICKED
                        msg.save(update_fields=['clicked_at', 'status'])
                EmailEvent.objects.create(
                    message=msg,
                    event_type=EmailEvent.EventType.CLICK,
                    url_clicked=destination_url,
                    ip_address=get_client_ip(request),
                    user_agent=det['user_agent'],
                    metadata={
                        'token': token,
                        'link_id': shortened_link.id,
                        'link_name': shortened_link.link_name,
                        'click_type': det['click_type']
                    }
                )

        response = HttpResponseRedirect(destination_url, status=302)
        response['Cache-Control'] = "no-cache, no-store, must-revalidate, max-age=0"
        return response


class CampaignLinkRedirectView(View):
    """
    Unified branded short-link endpoint.
    GET /c/:tracking_token (e.g. GET <tracking-base>/c/A7K29XQ2)

    Brevo-style intermediate redirect:
      1. Validate the token and resolve the campaign link.
      2. Record a click event (anonymous for SHAREABLE links,
         contact-attributed for RECIPIENT links) + counters.
      3. RECIPIENT links: update CampaignMessage / EmailEvent exactly
         like the legacy root-level recipient redirect.
      4. 302 redirect to the link destination (or campaign default),
         passing inbound query params (utm_*) through.
    """

    def get(self, request, token):
        from django.db.models import F
        from django.utils import timezone
        from .utils import (
            validate_destination_url, detect_bot_and_device, append_query_params,
        )
        token = (token or '').strip()
        link = CampaignTrackingLink.objects.select_related(
            'campaign', 'contact', 'shortened_link'
        ).filter(tracking_token=token).first()

        if not link:
            return render(request, 'tracking/link_error.html', {
                'error_message': 'The tracking link was not found or has expired.'
            }, status=404)

        if not link.is_active:
            return render(request, 'tracking/link_error.html', {
                'error_message': 'This tracking link has been disabled by the campaign owner.'
            }, status=410)

        campaign_status = (link.campaign.status or '').upper()
        if campaign_status in ('PAUSED', 'CANCELLED'):
            return render(request, 'tracking/link_error.html', {
                'error_message': 'This campaign is no longer active, so the tracking link is disabled.'
            }, status=410)

        destination_url = link.resolve_destination()
        if not validate_destination_url(destination_url):
            return render(request, 'tracking/link_error.html', {
                'error_message': 'No valid destination URL is configured for this tracking link yet.'
            }, status=400)

        det = detect_bot_and_device(request, None)
        now = timezone.now()

        # Race-safe counter updates (single UPDATE per statement).
        CampaignTrackingLink.objects.filter(
            pk=link.pk, first_clicked_at__isnull=True
        ).update(first_clicked_at=now)
        counter_field = (
            'bot_click_count' if det['click_type'] == 'SUSPECTED_BOT' else 'human_click_count'
        )
        CampaignTrackingLink.objects.filter(pk=link.pk).update(
            last_clicked_at=now,
            click_count=F('click_count') + 1,
            **{counter_field: F(counter_field) + 1},
        )

        is_recipient = (
            link.link_type == CampaignTrackingLink.LinkType.RECIPIENT
        )
        CampaignLinkClickEvent.objects.create(
            link=link,
            campaign=link.campaign,
            contact=link.contact if is_recipient else None,
            ip_address=get_client_ip(request),
            user_agent=det['user_agent'],
            browser=det['browser'],
            operating_system=det['operating_system'],
            device_type=det['device_type'],
            referrer=det['referrer'],
            click_type=det['click_type'],
            metadata={'reason': det['detection_reason']},
        )

        # Recipient attribution: mirror the legacy root-level redirect so
        # CampaignMessage CLICK status, EmailEvent CLICK rows and the
        # contact timeline behave identically for /c/ recipient URLs.
        if is_recipient and link.campaign_id and link.contact_id:
            msg = CampaignMessage.objects.filter(
                campaign_id=link.campaign_id,
                contact_id=link.contact_id,
            ).order_by('-sent_at', '-id').first()
            if msg:
                if det['click_type'] != 'SUSPECTED_BOT':
                    if not msg.clicked_at:
                        msg.clicked_at = now
                        msg.status = CampaignMessage.Status.CLICKED
                        msg.save(update_fields=['clicked_at', 'status'])
                EmailEvent.objects.create(
                    message=msg,
                    event_type=EmailEvent.EventType.CLICK,
                    url_clicked=destination_url,
                    ip_address=get_client_ip(request),
                    user_agent=det['user_agent'],
                    metadata={
                        'token': token,
                        'link_id': link.shortened_link_id,
                        'link_name': (link.shortened_link.link_name
                                      if link.shortened_link else None) or link.name,
                        'click_type': det['click_type'],
                    },
                )

        final_url = append_query_params(destination_url, request.META.get('QUERY_STRING', ''))
        response = HttpResponseRedirect(final_url, status=302)
        response['Cache-Control'] = "no-cache, no-store, must-revalidate, max-age=0"
        return response
