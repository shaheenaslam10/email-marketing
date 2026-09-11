from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Sender
from .serializers import SenderSerializer
from apps.email_providers.providers import get_email_provider
from apps.accounts.permissions import IsAdminOrSuperAdmin


class SenderViewSet(viewsets.ModelViewSet):
    queryset = Sender.objects.all().order_by('-created_at')
    serializer_class = SenderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_permissions(self):
        # Only Admins / Super Admins can modify sender accounts and credentials (Section 4)
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'test_connection']:
            return [IsAdminOrSuperAdmin()]
        return [permissions.IsAuthenticated()]

    def destroy(self, request, *args, **kwargs):
        sender = self.get_object()
        campaign_count = sender.campaigns.count()
        if campaign_count > 0:
            return Response(
                {'error': f'Cannot delete sender "{sender.name}" because it is currently linked to {campaign_count} campaign(s). Please reassign or delete those campaigns first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        sender_name = sender.name
        sender.delete()
        return Response({'status': 'success', 'message': f'Sender "{sender_name}" deleted successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        sender = self.get_object()
        provider = get_email_provider(sender)
        to_email = request.data.get('to_email')

        if to_email and str(to_email).strip():
            recipient = str(to_email).strip()
            subject = f"Test Email from {sender.name} ({sender.email})"
            html_content = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background-color: #ffffff;">
                    <div style="text-align: center; margin-bottom: 20px;">
                        <h2 style="color: #2563eb; margin: 0;">Test Email Delivery Successful!</h2>
                        <p style="color: #64748b; font-size: 13px; margin-top: 4px;">Sent from your Emailing Software platform</p>
                    </div>
                    <div style="background-color: #f8fafc; padding: 16px; border-radius: 8px; font-size: 13px; color: #334155; margin-bottom: 20px; border-left: 4px solid #2563eb;">
                        <p style="margin: 4px 0;"><strong>Sender Name:</strong> {sender.name}</p>
                        <p style="margin: 4px 0;"><strong>From Address:</strong> {sender.email}</p>
                        <p style="margin: 4px 0;"><strong>Provider:</strong> {sender.get_provider_type_display()}</p>
                        <p style="margin: 4px 0;"><strong>Host / Server:</strong> {sender.host or 'N/A'}</p>
                        <p style="margin: 4px 0;"><strong>Recipient:</strong> {recipient}</p>
                    </div>
                    <p style="color: #059669; font-weight: bold; font-size: 13px; margin: 0;">✓ If you are reading this email, your sender configuration and credentials are fully verified and working.</p>
                </div>
            """
            result = provider.send_email(
                to_email=recipient,
                subject=subject,
                html_content=html_content,
                text_content=f"Test email from {sender.name} ({sender.email}). Your sender configuration is verified and working.",
                reply_to=sender.reply_to
            )
            if result.success:
                return Response({'status': 'success', 'message': f'Test email successfully dispatched to {recipient}! Please check your inbox.'})
            return Response({'status': 'failed', 'message': f'Test email failed: {result.error_message}'}, status=status.HTTP_400_BAD_REQUEST)

        # Connection / auth handshake test if no recipient specified
        success, message = provider.test_connection()
        if success:
            return Response({'status': 'success', 'message': message})
        return Response({'status': 'failed', 'message': message}, status=status.HTTP_400_BAD_REQUEST)
