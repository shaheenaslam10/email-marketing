from rest_framework import permissions


class IsAdminOrSuperAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.can_manage_senders_and_keys)


class CanManageCampaigns(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.can_manage_campaigns)


class CanManageContacts(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.can_manage_contacts)
