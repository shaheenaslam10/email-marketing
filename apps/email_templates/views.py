from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import EmailTemplate
from .serializers import EmailTemplateSerializer


class EmailTemplateViewSet(viewsets.ModelViewSet):
    queryset = EmailTemplate.objects.all()
    serializer_class = EmailTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': 'No template IDs provided'}, status=status.HTTP_400_BAD_REQUEST)
        count, _ = EmailTemplate.objects.filter(id__in=ids).delete()
        return Response({'status': 'success', 'message': f'{count} template(s) deleted successfully.', 'deleted_count': count})

