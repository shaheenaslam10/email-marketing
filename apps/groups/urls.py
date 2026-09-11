from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ContactGroupViewSet, TestEmailGroupViewSet

router = DefaultRouter()
router.register(r'groups', ContactGroupViewSet, basename='group')
router.register(r'test-groups', TestEmailGroupViewSet, basename='test-group')

urlpatterns = [
    path('', include(router.urls)),
]
