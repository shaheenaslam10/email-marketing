from rest_framework import viewsets, permissions
from .models import SystemSetting
from .serializers import SystemSettingSerializer
from apps.accounts.permissions import IsAdminOrSuperAdmin


class SystemSettingViewSet(viewsets.ModelViewSet):
    queryset = SystemSetting.objects.all().order_by('key')
    serializer_class = SystemSettingSerializer
    permission_classes = [IsAdminOrSuperAdmin]
