from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from apps.odk.models import ODKConnection, ODKProject, ODKDataset, ODKEntity
from apps.odk.services import ODKClient, sync_dataset_entities
from apps.contacts.models import Contact
from apps.groups.models import ContactGroup

User = get_user_model()


class ODKEntityListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='odk_tester',
            email='odk_tester@example.com',
            password='testpassword123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.connection = ODKConnection.objects.create(
            name='Sandbox Connection',
            base_url='https://odk.mock.test',
            username='admin@example.com',
            is_mock_sandbox=True,
            is_active=True
        )

    def test_discover_datasets_service(self):
        client = ODKClient(self.connection)
        projects = client.discover_projects()
        self.assertTrue(len(projects) > 0)
        proj_id = projects[0]['id']

        datasets = client.discover_datasets(proj_id)
        self.assertTrue(len(datasets) >= 2)
        dataset_names = [d['name'] for d in datasets]
        self.assertIn('pulse_respondents_2026', dataset_names)

    def test_sync_dataset_entities(self):
        proj = ODKProject.objects.create(
            connection=self.connection,
            odk_id=1,
            name='Test Project'
        )
        dataset = ODKDataset.objects.create(
            project=proj,
            name='pulse_respondents_2026',
            description='Test Cohort'
        )
        count = sync_dataset_entities(dataset)
        self.assertEqual(count, 4)
        self.assertEqual(dataset.entities.count(), 4)

        entity = dataset.entities.filter(uuid='ent-001').first()
        self.assertIsNotNone(entity)
        self.assertEqual(entity.label, 'Ali Khan')
        self.assertEqual(entity.status, 'UNUSED')
        self.assertEqual(entity.data.get('email'), 'ali.khan@example.com')
        self.assertEqual(dataset.unused_count, 4)
        self.assertEqual(dataset.used_count, 0)


    def test_datasets_api_list_and_entities(self):
        proj = ODKProject.objects.create(
            connection=self.connection,
            odk_id=1,
            name='Test Project'
        )
        dataset = ODKDataset.objects.create(
            project=proj,
            name='pulse_respondents_2026',
            description='Test Cohort'
        )
        sync_dataset_entities(dataset)

        # List datasets
        res = self.client.get('/api/odk/datasets/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get('results', res.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'pulse_respondents_2026')

        # List entities for dataset
        res_entities = self.client.get(f'/api/odk/datasets/{dataset.id}/entities/')
        self.assertEqual(res_entities.status_code, status.HTTP_200_OK)
        ent_results = res_entities.data.get('results', res_entities.data)
        self.assertEqual(len(ent_results), 4)

    def test_import_entities_to_contacts(self):
        proj = ODKProject.objects.create(
            connection=self.connection,
            odk_id=1,
            name='Test Project'
        )
        dataset = ODKDataset.objects.create(
            project=proj,
            name='pulse_respondents_2026',
            description='Test Cohort'
        )
        sync_dataset_entities(dataset)

        group = ContactGroup.objects.create(name='Cohort A')

        res = self.client.post(
            f'/api/odk/datasets/{dataset.id}/import-to-contacts/',
            {'group_id': group.id},
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['created_count'], 4)
        self.assertEqual(res.data['total_imported'], 4)
        self.assertEqual(group.contacts.count(), 4)

        # Verify contact fields mapped properly
        contact = Contact.objects.get(email='ali.khan@example.com')
        self.assertEqual(contact.name, 'Ali Khan')
        self.assertEqual(contact.job_id, 'JOB-1025')
        self.assertEqual(contact.phone_number, '03001234567')

    def test_dataset_fields_endpoint(self):
        proj = ODKProject.objects.create(connection=self.connection, odk_id=1, name='Test Project')
        dataset = ODKDataset.objects.create(project=proj, name='test_fields_ds')
        ODKEntity.objects.create(
            dataset=dataset,
            uuid='ent-test-1',
            label='Sample Label',
            data={'p_name': 'Test User', 'respondent_email': 'tester@example.com', 'login': '03009998877'}
        )
        res = self.client.get(f'/api/odk/datasets/{dataset.id}/fields/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('p_name', res.data['fields'])
        self.assertIn('respondent_email', res.data['fields'])
        self.assertEqual(res.data['samples']['p_name'], 'Test User')
        self.assertEqual(res.data['samples']['respondent_email'], 'tester@example.com')

    def test_import_entities_with_custom_mapping(self):
        proj = ODKProject.objects.create(connection=self.connection, odk_id=1, name='Test Project')
        dataset = ODKDataset.objects.create(project=proj, name='test_custom_mapping_ds')
        ODKEntity.objects.create(
            dataset=dataset,
            uuid='ent-custom-1',
            label='A99',
            status='USED',
            data={
                'custom_person': 'Custom Name',
                'custom_mail': 'Custom Person <custom.person@example.com>',
                'custom_phone': '03112233445',
                'custom_secret': 'Secr3tP@ss'
            }
        )

        mapping = {
            'email': 'custom_mail',
            'name': 'custom_person',
            'job_id': 'label',
            'password': 'custom_secret',
            'phone': 'custom_phone'
        }

        res = self.client.post(
            f'/api/odk/datasets/{dataset.id}/import-to-contacts/',
            {'group_name': 'Custom Mapped Group', 'mapping': mapping},
            format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['created_count'], 1)

        c = Contact.objects.get(email='custom.person@example.com')
        self.assertEqual(c.name, 'Custom Name')
        self.assertEqual(c.job_id, 'A99')
        self.assertEqual(c.phone_number, '03112233445')
        self.assertEqual(c.status, Contact.UsageStatus.USED)
        self.assertEqual(c.login_password, 'Secr3tP@ss')

    def test_connection_discovery_endpoint(self):
        res = self.client.post(f'/api/odk/connections/{self.connection.id}/discover/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['status'], 'success')
        self.assertTrue(ODKDataset.objects.count() >= 2)
