from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SenderViewSet

router = DefaultRouter()
router.register(r'senders', SenderViewSet, basename='sender')

urlpatterns = [
    path('', include(router.urls)),
]
