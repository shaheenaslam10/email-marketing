from django.urls import path
from .views import GenericWebhookView

urlpatterns = [
    path('webhooks/<str:provider_name>/', GenericWebhookView.as_view(), name='email_webhook'),
]
