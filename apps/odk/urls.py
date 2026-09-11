from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ODKConnectionViewSet, ODKFormViewSet,
    ODKSyncJobViewSet, ODKUnmatchedSubmissionViewSet,
    ODKDatasetViewSet, ODKEntityViewSet
)

router = DefaultRouter()
router.register(r'connections', ODKConnectionViewSet, basename='odk-connection')
router.register(r'forms', ODKFormViewSet, basename='odk-form')
router.register(r'datasets', ODKDatasetViewSet, basename='odk-dataset')
router.register(r'entities', ODKEntityViewSet, basename='odk-entity')
router.register(r'sync-jobs', ODKSyncJobViewSet, basename='odk-sync-job')
router.register(r'unmatched-submissions', ODKUnmatchedSubmissionViewSet, basename='odk-unmatched-submission')

urlpatterns = [
    path('', include(router.urls)),
]

