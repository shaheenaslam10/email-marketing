from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from apps.contacts.models import Contact

User = get_user_model()


class ContactSourceAndUpdatedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='contact_tester',
            email='contact_tester@example.com',
            password='testpassword123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.c_odk = Contact.objects.create(
            name='ODK User',
            email='odk.user@example.com',
            status_source='ODK_CENTRAL'
        )
        self.c_import = Contact.objects.create(
            name='Excel User',
            email='excel.user@example.com',
            status_source='IMPORT'
        )
        self.c_manual = Contact.objects.create(
            name='Manual User',
            email='manual.user@example.com',
            status_source='MANUAL'
        )

    def test_contact_serialization_includes_source_and_updated_at(self):
        res = self.client.get('/api/contacts/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data)
        self.assertEqual(len(results), 3)
        item = results[0]
        self.assertIn('status_source', item)
        self.assertIn('updated_at', item)

    def test_filter_by_source(self):
        res_odk = self.client.get('/api/contacts/?source=ODK_CENTRAL')
        self.assertEqual(res_odk.status_code, status.HTTP_200_OK)
        results_odk = res_odk.data.get('results', res_odk.data)
        self.assertEqual(len(results_odk), 1)
        self.assertEqual(results_odk[0]['email'], 'odk.user@example.com')

        res_import = self.client.get('/api/contacts/?source=IMPORT')
        self.assertEqual(res_import.status_code, status.HTTP_200_OK)
        results_import = res_import.data.get('results', res_import.data)
        self.assertEqual(len(results_import), 1)
        self.assertEqual(results_import[0]['email'], 'excel.user@example.com')

    def test_sync_odk_endpoint_and_status_update(self):
        from apps.odk.models import ODKConnection, ODKProject, ODKDataset, ODKEntity
        from apps.odk.tasks import task_sync_odk_periodic

        conn = ODKConnection.objects.create(
            name='Test Sync Conn',
            base_url='https://odk.mock.test',
            is_mock_sandbox=True,
            is_active=True
        )
        proj = ODKProject.objects.create(connection=conn, odk_id=99, name='Sync Project')
        ds = ODKDataset.objects.create(project=proj, name='test_cohort', entities_count=1)
        ODKEntity.objects.create(
            dataset=ds,
            uuid='uuid-used-88',
            status='USED',
            data={'email': 'respondent88@example.com', 'used_at': '2026-09-09T10:00:00Z'}
        )

        contact = Contact.objects.create(
            email='respondent88@example.com',
            name='Respondent 88',
            job_id='uuid-used-88',
            status='UNUSED'
        )

        # 1. Test POST /api/contacts/sync-odk/
        res = self.client.post('/api/contacts/sync-odk/', format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('status', res.data)
        self.assertIn('contacts_marked_used', res.data)

        # Verify contact status was transitioned to USED
        contact.refresh_from_db()
        self.assertEqual(contact.status, Contact.UsageStatus.USED)
        self.assertEqual(contact.status_source, 'ODK_CENTRAL')
        self.assertIsNotNone(contact.odk_last_checked_at)

        # 2. Test Celery periodic task directly
        task_res = task_sync_odk_periodic()
        self.assertIn('status', task_res)
        self.assertIn(task_res['status'], ['success', 'warning'])

