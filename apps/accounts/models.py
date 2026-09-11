from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        SUPER_ADMIN = 'SUPER_ADMIN', 'Super Admin'
        ADMIN = 'ADMIN', 'Admin'
        CAMPAIGN_MANAGER = 'CAMPAIGN_MANAGER', 'Campaign Manager'
        VIEWER = 'VIEWER', 'Viewer'

    role = models.CharField(
        max_length=30,
        choices=Role.choices,
        default=Role.ADMIN,
        help_text="User role determining platform permissions."
    )
    phone = models.CharField(max_length=50, blank=True)

    @property
    def is_super_admin_role(self) -> bool:
        return self.is_superuser or self.role == self.Role.SUPER_ADMIN

    @property
    def can_manage_senders_and_keys(self) -> bool:
        return self.is_superuser or self.role in [self.Role.SUPER_ADMIN, self.Role.ADMIN]

    @property
    def can_manage_contacts(self) -> bool:
        return self.is_superuser or self.role in [self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.CAMPAIGN_MANAGER]

    @property
    def can_manage_campaigns(self) -> bool:
        return self.is_superuser or self.role in [self.Role.SUPER_ADMIN, self.Role.ADMIN, self.Role.CAMPAIGN_MANAGER]

    @property
    def can_view_reports(self) -> bool:
        return True  # All authenticated roles can view reports

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"
