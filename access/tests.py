import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase


from .models import AccessEventLog, AccountMapping, MappingModalState, PendingAccountMapping, Setting
from .views import ACCOUNT_MAPPING_PAGE_SIZE, EXTERNAL_ACCOUNTING_DB_NAME, get_test_users


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
