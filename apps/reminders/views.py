from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from .models import ReminderConfiguration, ReminderCycle
from .serializers import ReminderConfigurationSerializer, ReminderCycleSerializer
from .services import execute_reminder_cycle


class CampaignReminderView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, campaign_id):
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        if not hasattr(campaign, 'reminder_config'):
            return Response({'error': 'No reminder configuration'}, status=status.HTTP_404_NOT_FOUND)

        serializer = ReminderConfigurationSerializer(campaign.reminder_config)
        return Response(serializer.data)

    def post(self, request, campaign_id):
        """Updates reminder configuration settings."""
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        rem_cfg, _ = ReminderConfiguration.objects.get_or_create(campaign=campaign)
        serializer = ReminderConfigurationSerializer(rem_cfg, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ReminderPreviewEligibilityView(APIView):
    """
    Section 50: Before sending show:
    Selected Contacts
    Used and excluded
    Unused and eligible
    Unsubscribed
    Bounced
    Blocked
    Final Reminder Recipients
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, campaign_id):
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        groups = campaign.groups.all()
        all_contacts = Contact.objects.filter(groups__in=groups).distinct()

        total = all_contacts.count()
        used_excluded = all_contacts.filter(status=Contact.UsageStatus.USED).count()
        unsubscribed = all_contacts.filter(unsubscribed=True).count()
        bounced = all_contacts.filter(email_status__in=[Contact.EmailStatus.HARD_BOUNCE, Contact.EmailStatus.SOFT_BOUNCE]).count()
        blocked = all_contacts.filter(email_status=Contact.EmailStatus.BLOCKED).count()

        eligible = all_contacts.filter(
            status=Contact.UsageStatus.UNUSED,
            unsubscribed=False,
            email_status=Contact.EmailStatus.ACTIVE
        ).count()

        return Response({
            'selected_contacts': total,
            'used_and_excluded': used_excluded,
            'unsubscribed': unsubscribed,
            'bounced': bounced,
            'blocked': blocked,
            'unused_and_eligible': eligible,
            'final_recipients': eligible,
        })


class ReminderSendNowView(APIView):
    """Section 50: Trigger manual reminder cycle right now with pre-ODK sync."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, campaign_id):
        try:
            campaign = Campaign.objects.get(id=campaign_id)
        except Campaign.DoesNotExist:
            return Response({'error': 'Campaign not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            cycle = execute_reminder_cycle(campaign, manual_trigger=True)
            return Response({
                'status': 'completed',
                'cycle_number': cycle.cycle_number,
                'eligible_count': cycle.eligible_count,
                'sent_count': cycle.sent_count,
                'delivered_count': cycle.delivered_count,
                'executed_at': cycle.executed_at,
            })
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
