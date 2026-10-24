import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone


from .models import AccessControlDevice, AccessEventLog, AccessId, AccountMapping, MappingModalState, PendingAccountMapping, Setting, SystemMetadata
from .views import ACCOUNT_MAPPING_PAGE_SIZE, EXTERNAL_ACCOUNTING_DB_NAME, MAPPING_EVENT_PAGE_SIZE, get_test_users


class AccountMappingDetailViewTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='detail-operator')
		self.client.force_login(self.user)
		self.mapping = AccountMapping.objects.create(
			account_user_id='account/100',
			first_name='Detail',
			last_name='User',
			narration='Member details',
			usd_balance='0.00',
			zwg_balance='-15.25',
			accounting_group='main_bar',
		)
		self.url = reverse('account_mapping_detail', args=[self.mapping.pk])

	def test_detail_requires_login(self):
		self.client.logout()
		response = self.client.get(self.url)
		self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

	def test_detail_returns_404_for_missing_mapping(self):
		response = self.client.get(reverse('account_mapping_detail', args=[self.mapping.pk + 1]))
		self.assertEqual(response.status_code, 404)

	def test_detail_displays_account_and_empty_states_without_metadata(self):
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 200)
		self.assertTemplateUsed(response, 'access/mapping_detail.html')
		for text in (
			'Member details', 'account/100', 'Detail', 'User', 'Palladium',
			'Main Bar', '0.00', '-15.25', 'Not Connected',
			'No access IDs linked to this mapping.',
			'No access events recorded for this mapping.',
		):
			self.assertContains(response, text)
		self.assertFalse(response.context['connected'])
		self.assertEqual(response.context['event_stats'], {'total': 0, 'granted': 0, 'rejected': 0})
		self.assertIsNone(response.context['latest_event'])

	def test_detail_displays_all_connections_and_device_statuses(self):
		device = AccessControlDevice.objects.create(
			device_id='reader-100', alias='Front door', location='Reception',
			last_sync_at=timezone.now(),
		)
		inactive = AccessControlDevice.objects.create(device_id='reader-101', is_active=False)
		AccessId.objects.create(mapping=self.mapping, access_id='card-100', auth_type='card', access_device=device)
		AccessId.objects.create(mapping=self.mapping, access_id='bio-100', auth_type='biometric', access_device=inactive)
		AccessId.objects.create(mapping=self.mapping, access_id='pin-100', auth_type='pin')

		response = self.client.get(self.url)
		self.assertTrue(response.context['connected'])
		self.assertEqual(len(response.context['access_ids']), 3)
		for text in (
			'Connected', 'card-100', 'bio-100', 'pin-100', 'Card', 'Biometric', 'PIN',
			'Front door', 'reader-100', 'Reception', 'Active', 'Inactive',
			'Not assigned', 'Unknown', 'No sync recorded',
			device.last_sync_at.strftime('%Y-%m-%d'),
		):
			self.assertContains(response, text)

	def test_blank_ids_and_missing_balances_do_not_imply_connection_or_zero(self):
		AccessId.objects.create(mapping=self.mapping, access_id='')
		AccessId.objects.create(mapping=self.mapping, access_id=None)
		self.mapping.usd_balance = None
		self.mapping.zwg_balance = None
		self.mapping.save()
		response = self.client.get(self.url)
		self.assertFalse(response.context['connected'])
		self.assertContains(response, 'Not Connected')
		self.assertContains(response, 'Not available', count=4)

	def test_history_uses_recorded_mapping_not_current_access_id_ownership(self):
		other = AccountMapping.objects.create(account_user_id='other-account')
		device = AccessControlDevice.objects.create(device_id='event-reader', alias='Event reader')
		AccessId.objects.create(mapping=self.mapping, access_id='shared-card')
		reject = AccessEventLog.objects.create(
			user=self.mapping, device_access_id='old-card', access_status='reject',
			access_type='one_step_mapping', access_control_device=device,
		)
		grant = AccessEventLog.objects.create(
			user=self.mapping, device_access_id='shared-card', access_status='grant',
		)
		AccessEventLog.objects.create(user=other, device_access_id='shared-card', access_status='reject')
		AccessEventLog.objects.create(device_access_id='shared-card', access_status='reject')

		response = self.client.get(self.url)
		self.assertEqual(response.context['event_stats'], {'total': 2, 'granted': 1, 'rejected': 1})
		self.assertEqual(list(response.context['page_obj']), [grant, reject])
		self.assertEqual(response.context['latest_event'], grant)
		for text in ('old-card', 'One-Step Mapping Attempt', 'Event reader', 'Unknown device'):
			self.assertContains(response, text)

	def test_history_paginates_with_stable_order_and_whole_history_counts(self):
		events = [
			AccessEventLog.objects.create(user=self.mapping, device_access_id=f'log-{index}', access_status='grant')
			for index in range(MAPPING_EVENT_PAGE_SIZE + 2)
		]
		AccessEventLog.objects.filter(user=self.mapping).update(event_timestamp=timezone.now())
		response = self.client.get(self.url, {'page': 2})
		self.assertEqual(response.context['page_obj'].number, 2)
		self.assertEqual(list(response.context['page_obj']), [events[1], events[0]])
		self.assertEqual(response.context['event_stats']['total'], MAPPING_EVENT_PAGE_SIZE + 2)
		self.assertContains(response, '?page=1')
		for page, expected in [('invalid', 1), ('999', 2)]:
			with self.subTest(page=page):
				response = self.client.get(self.url, {'page': page})
				self.assertEqual(response.context['page_obj'].number, expected)

	def test_mapping_list_links_to_detail(self):
		SystemMetadata.objects.create()
		response = self.client.get(reverse('account_mapping_list'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, f'href="{self.url}"')

	def test_detail_escapes_external_values(self):
		self.mapping.narration = '<script>alert("member")</script>'
		self.mapping.save()
		response = self.client.get(self.url)
		self.assertContains(response, '&lt;script&gt;')
		self.assertNotContains(response, '<script>alert("member")</script>')


class ExternalAccountingDbMixin:
	def create_external_accounting_db(self, base_dir: Path, users: list[tuple[str, str, float]]) -> Path:
		db_path = Path(base_dir) / EXTERNAL_ACCOUNTING_DB_NAME
		with closing(sqlite3.connect(db_path)) as conn:
			cursor = conn.cursor()
			cursor.execute('DROP TABLE IF EXISTS accounting_users')
			cursor.execute(
				'''
				CREATE TABLE accounting_users (
					account_user_id TEXT PRIMARY KEY,
					full_name TEXT NOT NULL,
					balance REAL NOT NULL
				)
				'''
			)
			cursor.executemany(
				'''
				INSERT INTO accounting_users (account_user_id, full_name, balance)
				VALUES (?, ?, ?)
				''',
				users,
			)
			conn.commit()
		return db_path


class GetTestUsersTests(TestCase, ExternalAccountingDbMixin):
	def test_get_test_users_returns_empty_list_when_external_db_missing(self):
		with tempfile.TemporaryDirectory() as tmpdir:
			with self.settings(BASE_DIR=Path(tmpdir)):
				users = get_test_users()
		self.assertEqual(users, [])

	def test_get_test_users_merges_external_users_with_mapping_state(self):
		with tempfile.TemporaryDirectory() as tmpdir:
			with self.settings(BASE_DIR=Path(tmpdir)):
				self.create_external_accounting_db(
					Path(tmpdir),
					[
						('200', 'Alpha User', 12.34),
						('100', 'Zulu User', -4.2),
					],
				)
				AccountMapping.objects.create(
					account_user_id='200',
					device_access_id='A-200',
					accounting_system='palladium',
				)

				users = get_test_users()

		self.assertEqual(len(users), 2)
		self.assertEqual(users[0]['full_name'], 'Alpha User')
		self.assertEqual(users[0]['device_access_id'], 'A-200')
		self.assertTrue(users[0]['connected'])
		self.assertIsNone(users[1]['device_access_id'])
		self.assertFalse(users[1]['connected'])


class AccountMappingListViewTests(TestCase):
	def test_account_mapping_list_view_shows_sync_button_and_last_updated(self):
		AccountMapping.objects.create(
			account_user_id='900',
			first_name='Sync',
			last_name='User',
			accounting_system='palladium',
		)

		response = self.client.get('/')

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Sync Users')
		self.assertContains(response, 'Last updated:')

	def test_account_mapping_list_view_paginates_results(self):
		for index in range(ACCOUNT_MAPPING_PAGE_SIZE + 2):
			AccountMapping.objects.create(
				account_user_id=f'{index:03d}',
				first_name='User',
				last_name=f'{index:02d}',
				accounting_system='palladium',
			)

		response = self.client.get('/?page=2')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['page_obj'].number, 2)
		self.assertTrue(response.context['page_obj'].has_previous())
		self.assertFalse(response.context['page_obj'].has_next())
		self.assertEqual(len(response.context['users']), 2)
		self.assertEqual(
			[mapping.account_user_id for mapping in response.context['users']],
			['001', '000'],
		)

	def test_account_mapping_list_view_filters_connected_users(self):
		AccountMapping.objects.create(
			account_user_id='100',
			first_name='Connected',
			last_name='User',
			device_access_id='A-100',
			accounting_system='palladium',
		)
		AccountMapping.objects.create(
			account_user_id='101',
			first_name='Not',
			last_name='Connected',
			device_access_id=None,
			accounting_system='palladium',
		)

		response = self.client.get('/?connection_status=connected')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.context['users']), 1)
		self.assertEqual(response.context['users'][0].account_user_id, '100')


class AccessEventViewTests(TestCase, ExternalAccountingDbMixin):
	def setUp(self):
		self.settings_obj = Setting.get_solo()
		MappingModalState.objects.update_or_create(id=1, defaults={'state': 'closed'})

	def test_access_event_rejects_and_stores_pending_when_modal_open(self):
		MappingModalState.objects.update_or_create(id=1, defaults={'state': 'open'})

		response = self.client.post(
			'/api/access',
			data=json.dumps({'access_id': 'D-100'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'access': 'REJECT'})
		self.assertEqual(PendingAccountMapping.objects.count(), 1)
		self.assertEqual(PendingAccountMapping.objects.first().device_access_id, 'D-100')
		self.assertEqual(AccessEventLog.objects.count(), 1)
		self.assertEqual(AccessEventLog.objects.first().access_status, 'reject')

	def test_access_event_grants_when_authorization_flow_is_grant_all(self):
		self.settings_obj.authorization_flow = 'grant_all'
		self.settings_obj.save()

		response = self.client.post(
			'/api/access',
			data=json.dumps({'access_id': 'D-200'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'access': 'GRANT'})
		self.assertEqual(AccessEventLog.objects.count(), 1)
		self.assertEqual(AccessEventLog.objects.first().access_status, 'grant')

	def test_access_event_rejects_when_authorization_flow_is_reject_all(self):
		self.settings_obj.authorization_flow = 'reject_all'
		self.settings_obj.save()

		response = self.client.post(
			'/api/access',
			data=json.dumps({'access_id': 'D-201'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'access': 'REJECT'})
		self.assertEqual(AccessEventLog.objects.count(), 1)
		self.assertEqual(AccessEventLog.objects.first().access_status, 'reject')

	def test_access_event_check_balance_grants_when_balance_meets_threshold(self):
		self.settings_obj.authorization_flow = 'check_balance'
		self.settings_obj.balance_threshold = 0
		self.settings_obj.save()

		AccountMapping.objects.create(
			account_user_id='700',
			device_access_id='D-700',
			accounting_system='palladium',
		)

		with tempfile.TemporaryDirectory() as tmpdir:
			with self.settings(BASE_DIR=Path(tmpdir)):
				self.create_external_accounting_db(
					Path(tmpdir),
					[('700', 'Balance User', 11.5)],
				)

				response = self.client.post(
					'/api/access',
					data=json.dumps({'access_id': 'D-700'}),
					content_type='application/json',
				)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'access': 'GRANT'})
		log = AccessEventLog.objects.get(device_access_id='D-700')
		self.assertEqual(log.access_status, 'grant')
		self.assertEqual(log.account_user_id, '700')

	def test_access_event_check_balance_rejects_when_balance_below_threshold(self):
		self.settings_obj.authorization_flow = 'check_balance'
		self.settings_obj.balance_threshold = 20
		self.settings_obj.save()

		AccountMapping.objects.create(
			account_user_id='701',
			device_access_id='D-701',
			accounting_system='palladium',
		)

		with tempfile.TemporaryDirectory() as tmpdir:
			with self.settings(BASE_DIR=Path(tmpdir)):
				self.create_external_accounting_db(
					Path(tmpdir),
					[('701', 'Low Balance User', 5.0)],
				)

				response = self.client.post(
					'/api/access',
					data=json.dumps({'access_id': 'D-701'}),
					content_type='application/json',
				)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'access': 'REJECT'})
		log = AccessEventLog.objects.get(device_access_id='D-701')
		self.assertEqual(log.access_status, 'reject')
		self.assertEqual(log.account_user_id, '701')


class ApiMappingViewTests(TestCase):
	def test_get_pending_mapping_returns_latest_pending_access_id(self):
		PendingAccountMapping.objects.create(device_access_id='P-1')

		response = self.client.get('/api/mappings/pending')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'device_access_id': 'P-1'})

	def test_post_mapping_creates_mapping_and_clears_pending(self):
		PendingAccountMapping.objects.create(device_access_id='P-2')

		response = self.client.post(
			'/api/mappings',
			data=json.dumps({'account_user_id': '300', 'device_access_id': 'D-300'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 201)
		self.assertEqual(response.json(), {'status': 'created'})
		self.assertTrue(AccountMapping.objects.filter(account_user_id='300', device_access_id='D-300').exists())
		self.assertEqual(PendingAccountMapping.objects.count(), 0)

	def test_post_mapping_rejects_duplicate_device_access_id(self):
		AccountMapping.objects.create(
			account_user_id='301',
			device_access_id='D-dup',
			accounting_system='palladium',
		)

		response = self.client.post(
			'/api/mappings',
			data=json.dumps({'account_user_id': '302', 'device_access_id': 'D-dup'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertIn('already mapped', response.json()['error'])

	def test_delete_mapping_unmaps_user(self):
		AccountMapping.objects.create(
			account_user_id='303',
			device_access_id='D-303',
			accounting_system='palladium',
		)

		response = self.client.delete('/api/mappings/303')

		self.assertEqual(response.status_code, 200)
		mapping = AccountMapping.objects.get(account_user_id='303')
		self.assertIsNone(mapping.device_access_id)


class ModalStateViewTests(TestCase):
	def test_set_modal_state_closed_clears_pending_mappings(self):
		PendingAccountMapping.objects.create(device_access_id='TEMP-1')

		response = self.client.post(
			'/api/modal-state',
			data=json.dumps({'state': 'closed'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 201)
		self.assertEqual(response.json(), {'status': 'created'})
		self.assertEqual(PendingAccountMapping.objects.count(), 0)
		self.assertEqual(MappingModalState.objects.get(id=1).state, 'closed')


class LoadInitialDataCommandTests(TestCase):
	def test_load_initial_data_creates_external_db_and_bootstraps_mappings(self):
		with tempfile.TemporaryDirectory() as tmpdir:
			with self.settings(BASE_DIR=Path(tmpdir)):
				call_command('load_initial_data')

				external_db = Path(tmpdir) / EXTERNAL_ACCOUNTING_DB_NAME
				self.assertTrue(external_db.exists())

				with closing(sqlite3.connect(external_db)) as conn:
					cols = [row[1] for row in conn.execute('PRAGMA table_info(accounting_users)').fetchall()]
					self.assertEqual(cols, ['account_user_id', 'full_name', 'balance'])

				mapping = AccountMapping.objects.get(account_user_id='904738')
				self.assertEqual(mapping.device_access_id, '175750')
				self.assertIn(mapping.accounting_system, {'palladium', 'quickbooks', 'xero'})
				self.assertEqual(MappingModalState.objects.get(id=1).state, 'closed')


class TemplateViewAuthenticationTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(
			username='admin_user',
			password='StrongTestPass123!',
		)

	def test_mapping_list_requires_authentication(self):
		response = self.client.get('/')

		self.assertEqual(response.status_code, 302)
		self.assertIn('/login/?next=/', response.url)

	def test_settings_requires_authentication(self):
		response = self.client.get('/settings/')

		self.assertEqual(response.status_code, 302)
		self.assertIn('/login/?next=/settings/', response.url)

	def test_authenticated_user_can_access_template_views(self):
		self.client.force_login(self.user)

		list_response = self.client.get('/')
		settings_response = self.client.get('/settings/')

		self.assertEqual(list_response.status_code, 200)
		self.assertEqual(settings_response.status_code, 200)
		self.assertContains(list_response, 'admin_user')

	def test_successful_login_shows_welcome_message(self):
		response = self.client.post(
			'/login/',
			data={'username': 'admin_user', 'password': 'StrongTestPass123!'},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Welcome back, admin_user.')

	def test_logout_shows_info_message(self):
		self.client.force_login(self.user)

		response = self.client.post('/logout/', follow=True)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'You have been logged out.')
