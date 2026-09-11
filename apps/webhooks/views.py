import json
import logging
from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from apps.campaigns.models import CampaignMessage
from apps.contacts.models import Contact
from apps.tracking.models import EmailEvent

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class GenericWebhookView(View):
    """
    Receives bounce and delivery status notifications from email providers.
    (Section 74)
    """

    def post(self, request, provider_name):
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = request.POST.dict()

        logger.info("Received webhook from %s: %s", provider_name, data)

        event_type = data.get('event') or data.get('type') or ''
        msg_id = data.get('message_id') or data.get('id') or ''
        recipient = data.get('recipient') or data.get('email') or ''

        # Map generic status
        if any(x in str(event_type).lower() for x in ['hard_bounce', 'hardbounce', 'bounce', 'failed']):
            status = 'HARD_BOUNCE'
        elif any(x in str(event_type).lower() for x in ['soft_bounce', 'softbounce']):
            status = 'SOFT_BOUNCE'
        elif any(x in str(event_type).lower() for x in ['delivered']):
            status = 'DELIVERED'
        elif any(x in str(event_type).lower() for x in ['complaint', 'spam']):
            status = 'COMPLAINT'
        else:
            status = 'EVENT'

        # Look up message by provider_message_id
        message = None
        if msg_id:
            message = CampaignMessage.objects.filter(provider_message_id=msg_id).first()

        # Look up contact
        contact = None
        if message:
            contact = message.contact
        elif recipient:
            contact = Contact.objects.filter(email=recipient).first()

        if contact:
            if status == 'HARD_BOUNCE':
                contact.email_status = Contact.EmailStatus.HARD_BOUNCE
                contact.bounce_status = 'Hard bounce reported by provider'
                contact.save(update_fields=['email_status', 'bounce_status'])
            elif status == 'COMPLAINT':
                contact.email_status = Contact.EmailStatus.BLOCKED
                contact.save(update_fields=['email_status'])

        if message:
            if status in ['HARD_BOUNCE', 'SOFT_BOUNCE', 'DELIVERED']:
                message.status = status
                message.save(update_fields=['status'])
            EmailEvent.objects.create(
                message=message,
                event_type=status,
                metadata={'provider': provider_name, 'raw': data}
            )

        return JsonResponse({'status': 'received', 'provider': provider_name})
