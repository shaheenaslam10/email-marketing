from django.db.models import Q
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Contact, ContactStatusHistory, ContactCustomField, ContactCustomValue
from .serializers import (
    ContactSerializer, ContactStatusHistorySerializer,
    ContactCustomFieldSerializer
)
from apps.groups.models import ContactGroup


class ContactViewSet(viewsets.ModelViewSet):
    queryset = Contact.objects.all().order_by('-created_at')
    serializer_class = ContactSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param.upper())
        
        email_status = self.request.query_params.get('email_status')
        if email_status:
            qs = qs.filter(email_status=email_status)

        source_param = self.request.query_params.get('source') or self.request.query_params.get('status_source')
        if source_param:
            qs = qs.filter(status_source__iexact=source_param)

        group_id = self.request.query_params.get('group')
        if group_id:
            qs = qs.filter(groups__id=group_id)

        groups_param = self.request.query_params.get('groups')
        if groups_param:
            g_ids = [int(x) for x in str(groups_param).split(',') if x.strip().isdigit()]
            if g_ids:
                qs = qs.filter(groups__id__in=g_ids).distinct()

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone_number__icontains=search) |
                Q(job_id__icontains=search)
            )
        return qs

    def list(self, request, *args, **kwargs):
        """Override list to inject group_custom_values when filtering by group."""
        response = super().list(request, *args, **kwargs)
        group_id = request.query_params.get('group')
        if group_id and response.data.get('results'):
            from apps.groups.models import GroupContactValue
            contact_ids = [c['id'] for c in response.data['results']]
            # Batch load all group custom values for visible contacts
            values = GroupContactValue.objects.filter(
                contact_id__in=contact_ids,
                field__group_id=group_id
            ).select_related('field')
            # Build a lookup: {contact_id: [{field_slug, field_name, value}]}
            value_map = {}
            for v in values:
                if v.contact_id not in value_map:
                    value_map[v.contact_id] = []
                value_map[v.contact_id].append({
                    'field_slug': v.field.slug,
                    'field_name': v.field.name,
                    'value': v.value
                })
            for contact_data in response.data['results']:
                contact_data['group_custom_values'] = value_map.get(contact_data['id'], [])
        return response

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        contact = self.get_object()
        history = contact.status_history.all()
        serializer = ContactStatusHistorySerializer(history, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        """Unified Contact Activity Timeline (Requirement 11)."""
        contact = self.get_object()
        events = []

        # 1. Campaign & Reminder email dispatches
        for msg in contact.campaign_messages.select_related('campaign').all():
            event_time = msg.sent_at or msg.delivered_at or msg.created_at
            if msg.message_type == 'REMINDER':
                title = f"Reminder email sent (Cycle #{msg.reminder_sequence})"
                evt_type = 'REMINDER_SENT'
            else:
                title = "Campaign email sent"
                evt_type = 'CAMPAIGN_SENT'

            events.append({
                'timestamp': event_time.isoformat() if event_time else None,
                'time_val': event_time,
                'type': evt_type,
                'title': title,
                'description': f"Campaign: {msg.campaign.name} &bull; Subject: {msg.subject}",
                'badge': 'indigo' if evt_type == 'REMINDER_SENT' else 'blue',
                'icon': 'send'
            })

            if msg.opened_at:
                events.append({
                    'timestamp': msg.opened_at.isoformat(),
                    'time_val': msg.opened_at,
                    'type': 'EMAIL_OPENED',
                    'title': 'Campaign email opened',
                    'description': f"Campaign: {msg.campaign.name}",
                    'badge': 'purple',
                    'icon': 'mail-open'
                })

        # 2. Link Click Events
        from apps.tracking.models import LinkClickEvent, CampaignLinkClickEvent
        for ce in LinkClickEvent.objects.filter(contact=contact).select_related('recipient_link__shortened_link', 'campaign'):
            sl = ce.recipient_link.shortened_link
            link_name = sl.link_name or 'Tracked Link'
            bot_str = " (Suspected Bot)" if ce.click_type == 'SUSPECTED_BOT' else ""
            events.append({
                'timestamp': ce.clicked_at.isoformat(),
                'time_val': ce.clicked_at,
                'type': 'LINK_CLICK',
                'title': f"{link_name} clicked{bot_str}",
                'description': f"Destination: {sl.original_url} &bull; Device: {ce.device_type or 'Unknown'} &bull; Browser: {ce.browser or 'Unknown'}",
                'badge': 'emerald' if ce.click_type == 'HUMAN' else 'amber',
                'icon': 'external-link'
            })

        # Recipient /c/ link visits (same shape as legacy link clicks).
        for ce in CampaignLinkClickEvent.objects.filter(contact=contact).select_related('link__shortened_link', 'campaign'):
            sl = ce.link.shortened_link
            link_name = (sl.link_name if sl else None) or ce.link.name or 'Tracked Link'
            dest = sl.original_url if sl else (ce.link.destination_url or '')
            bot_str = " (Suspected Bot)" if ce.click_type == 'SUSPECTED_BOT' else ""
            events.append({
                'timestamp': ce.clicked_at.isoformat(),
                'time_val': ce.clicked_at,
                'type': 'LINK_CLICK',
                'title': f"{link_name} clicked{bot_str}",
                'description': f"Destination: {dest} &bull; Device: {ce.device_type or 'Unknown'} &bull; Browser: {ce.browser or 'Unknown'}",
                'badge': 'emerald' if ce.click_type == 'HUMAN' else 'amber',
                'icon': 'external-link'
            })

        # 3. Status Transition History
        for h in contact.status_history.all():
            src_str = "ODK" if "ODK" in (h.source or "").upper() else (h.source or "Manual")
            events.append({
                'timestamp': h.changed_at.isoformat(),
                'time_val': h.changed_at,
                'type': 'STATUS_CHANGE',
                'title': f"{src_str} Status changed from {h.old_status.lower()} to {h.new_status.lower()}",
                'description': f"Source: {h.source}" + (f" (ODK Submission: {h.odk_submission_id})" if h.odk_submission_id else ""),
                'badge': 'teal' if h.new_status == 'USED' else 'slate',
                'icon': 'check-circle' if h.new_status == 'USED' else 'clock'
            })

        # Sort chronologically descending
        from django.utils import timezone
        events.sort(key=lambda x: x['time_val'] or timezone.now(), reverse=True)
        for e in events:
            del e['time_val']

        return Response(events)

    @action(detail=False, methods=['post'], url_path='sync-odk')
    def sync_odk(self, request):
        """Unified sync trigger from Contacts page: syncs forms and datasets from ODK Central."""
        from apps.odk.services import sync_all_odk_contacts
        try:
            result = sync_all_odk_contacts(trigger_source='MANUAL', user=request.user if request.user.is_authenticated else None)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'status': 'error', 'message': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], url_path='sync-status')
    def sync_status(self, request):
        """Returns the latest ODK sync timestamp for the UI indicator."""
        from apps.odk.models import ODKDataset, ODKSyncJob
        latest_dataset = ODKDataset.objects.filter(last_entity_at__isnull=False).order_by('-last_entity_at').first()
        latest_job = ODKSyncJob.objects.filter(completed_at__isnull=False).order_by('-completed_at').first()
        last_time = None
        if latest_dataset and latest_dataset.last_entity_at:
            last_time = latest_dataset.last_entity_at
        if latest_job and latest_job.completed_at:
            if not last_time or latest_job.completed_at > last_time:
                last_time = latest_job.completed_at
        return Response({
            'last_synced_at': last_time.isoformat() if last_time else None
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='bulk-action')
    def bulk_action(self, request):
        action_type = request.data.get('action')
        select_all = request.data.get('select_all', False)
        contact_ids = request.data.get('contact_ids', [])
        
        if select_all:
            contacts = self.get_queryset()
            status_param = request.data.get('status')
            if status_param:
                contacts = contacts.filter(status=status_param.upper())
            email_status = request.data.get('email_status')
            if email_status:
                contacts = contacts.filter(email_status=email_status)
            source_param = request.data.get('source') or request.data.get('status_source')
            if source_param:
                contacts = contacts.filter(status_source__iexact=source_param)
            group_id = request.data.get('group') or request.data.get('filter_group')
            if group_id:
                contacts = contacts.filter(groups__id=group_id)
            search = request.data.get('search')
            if search:
                contacts = contacts.filter(
                    Q(name__icontains=search) |
                    Q(email__icontains=search) |
                    Q(phone_number__icontains=search) |
                    Q(job_id__icontains=search)
                )
        elif contact_ids:
            contacts = Contact.objects.filter(id__in=contact_ids)
        else:
            return Response({'error': 'No contact IDs provided or select_all specified'}, status=status.HTTP_400_BAD_REQUEST)

        count = contacts.count()
        if count == 0:
            return Response({'message': 'No contacts matched selection', 'count': 0})

        if action_type == 'mark_used':
            to_update = list(contacts.exclude(status='USED').values_list('id', 'status'))
            if to_update:
                Contact.objects.filter(id__in=[c[0] for c in to_update]).update(status='USED', status_source='MANUAL')
                histories = [
                    ContactStatusHistory(
                        contact_id=cid,
                        old_status=old_s,
                        new_status='USED',
                        source='MANUAL',
                        changed_by=request.user if request.user.is_authenticated else None,
                        notes='Bulk manual update'
                    )
                    for cid, old_s in to_update
                ]
                ContactStatusHistory.objects.bulk_create(histories)
            return Response({'message': f'Marked {count} contact{"s" if count != 1 else ""} as USED', 'count': count})

        elif action_type == 'mark_unused':
            to_update = list(contacts.exclude(status='UNUSED').values_list('id', 'status'))
            if to_update:
                Contact.objects.filter(id__in=[c[0] for c in to_update]).update(status='UNUSED', status_source='MANUAL')
                histories = [
                    ContactStatusHistory(
                        contact_id=cid,
                        old_status=old_s,
                        new_status='UNUSED',
                        source='MANUAL',
                        changed_by=request.user if request.user.is_authenticated else None,
                        notes='Bulk manual update'
                    )
                    for cid, old_s in to_update
                ]
                ContactStatusHistory.objects.bulk_create(histories)
            return Response({'message': f'Marked {count} contact{"s" if count != 1 else ""} as UNUSED', 'count': count})

        elif action_type == 'add_to_group':
            group_id = request.data.get('group_id')
            if not group_id:
                return Response({'error': 'group_id required'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                group = ContactGroup.objects.get(id=group_id)
                group.contacts.add(*contacts)
                return Response({'message': f'Added {count} contact{"s" if count != 1 else ""} to {group.name}', 'count': count})
            except ContactGroup.DoesNotExist:
                return Response({'error': 'Group not found'}, status=status.HTTP_404_NOT_FOUND)

        elif action_type == 'remove_from_group':
            group_id = request.data.get('group_id')
            if not group_id:
                return Response({'error': 'group_id required'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                group = ContactGroup.objects.get(id=group_id)
                group.contacts.remove(*contacts)
                return Response({'message': f'Removed {count} contact{"s" if count != 1 else ""} from {group.name}', 'count': count})
            except ContactGroup.DoesNotExist:
                return Response({'error': 'Group not found'}, status=status.HTTP_404_NOT_FOUND)

        elif action_type == 'unsubscribe':
            contacts.update(unsubscribed=True, email_status='Unsubscribed')
            return Response({'message': f'Unsubscribed {count} contact{"s" if count != 1 else ""}', 'count': count})

        elif action_type == 'delete':
            contacts.delete()
            return Response({'message': f'Deleted {count} contact{"s" if count != 1 else ""}', 'count': count})

        return Response({'error': f'Unsupported action {action_type}'}, status=status.HTTP_400_BAD_REQUEST)


class ContactCustomFieldViewSet(viewsets.ModelViewSet):
    queryset = ContactCustomField.objects.all().order_by('name')
    serializer_class = ContactCustomFieldSerializer
    permission_classes = [permissions.IsAuthenticated]
