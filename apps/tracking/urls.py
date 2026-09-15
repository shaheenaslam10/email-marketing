from django.urls import path, re_path
from .views import (
    OpenTrackingView, ClickTrackingView, UnsubscribeView,
    ShortUrlRedirectView, CampaignLinkRedirectView,
)

urlpatterns = [
    path('t/open/<uuid:token>/', OpenTrackingView.as_view(), name='track_open'),
    path('t/click/<uuid:token>/', ClickTrackingView.as_view(), name='track_click'),
    path('t/unsubscribe/<uuid:token>/', UnsubscribeView.as_view(), name='track_unsubscribe'),
    # Campaign-level shareable tracking links (must precede the root catch-all).
    re_path(r'^c/(?P<token>[A-Za-z0-9]{6,16})/?$', CampaignLinkRedirectView.as_view(), name='campaign_link_redirect'),
    re_path(r'^(?P<token>[A-Za-z0-9]{6,16})/?$', ShortUrlRedirectView.as_view(), name='short_url_redirect'),
]
