from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import UserViewSet, CurrentUserView, LoginView, LogoutView

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')

urlpatterns = [
    path('', include(router.urls)),
    path('me/', CurrentUserView.as_view(), name='current_user'),
    # Backwards compatibility
    path('api/', include(router.urls)),
    path('api/me/', CurrentUserView.as_view(), name='current_user_compat'),
]
