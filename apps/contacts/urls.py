from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ContactViewSet, ContactCustomFieldViewSet

router = DefaultRouter()
router.register(r'contacts', ContactViewSet, basename='contact')
router.register(r'custom-fields', ContactCustomFieldViewSet, basename='custom-field')

urlpatterns = [
    path('', include(router.urls)),
]
