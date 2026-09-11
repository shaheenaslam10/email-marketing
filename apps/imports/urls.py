from django.urls import path
from .views import ImportUploadView, ImportAnalyzeView, ImportExecuteView

urlpatterns = [
    path('upload/', ImportUploadView.as_view(), name='import_upload'),
    path('<int:pk>/analyze/', ImportAnalyzeView.as_view(), name='import_analyze'),
    path('<int:pk>/execute/', ImportExecuteView.as_view(), name='import_execute'),
]
