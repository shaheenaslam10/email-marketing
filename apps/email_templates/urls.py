from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EmailTemplateViewSet

router = DefaultRouter()
router.register(r'templates', EmailTemplateViewSet, basename='template')

urlpatterns = [
    path('', include(router.urls)),
]
