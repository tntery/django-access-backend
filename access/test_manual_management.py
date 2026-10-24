import json
from io import StringIO
from unittest.mock import Mock, mock_open, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from . import views
from .models import AccessControlDevice, AccessEventLog, AccessId, AccountMapping, Setting, SystemMetadata


class ManualMappingManagementTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username='mapping-admin', is_staff=True)
        self.client.force_login(self.staff)
        self.mapping = AccountMapping.objects.create(account_user_id='external-100', narration='External Member')
        self.device = AccessControlDevice.objects.create(device_id='manual-reader')
        self.config = Setting.get_solo()
        self.config.authorization_flow = 'grant_all'
        self.config.save()
        self.addCleanup(patch.stopall)
        patch.object(views, 'access_decisions_list', []).start()
        patch.object(views, 'STAFF_USER_IDS', []).start()

    def connect(self, access_id='manual-card', device=None, mapping=None, auth_type='card'):
        return self.client.post(reverse('mapping_access_id_create', args=[(mapping or self.mapping).pk]), {
            'access_id': access_id, 'auth_type': auth_type, 'access_device': device.pk if device else '',
        })

    def external_user(self, account=None, card='manual-card'):
        return {
            'accountNumber': account or self.mapping.account_user_id, 'cardId': card,
            'memberName': 'Synced Name', 'firstName': 'Synced', 'lastName': 'User',
            'usdBalance': 20, 'zwdBalance': 30, 'classification': 'member',
            'is_manual': False, 'access_override': 'auto',
        }

    def sync(self, users):
        response = Mock()
        response.json.return_value = users
        with patch.object(views.requests, 'get', return_value=response), patch('access.views.open', mock_open()):
            result = views.sync_external_users()
        self.assertTrue(result['ok'])

    def event(self, access_id):
        response = self.client.post(
            reverse('access_event', args=[self.device.device_id]),
            json.dumps({'access_id': access_id}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return response.json()['access']

    def cached(self):
        response = self.client.get(reverse('access_event', args=[self.device.device_id]))
        self.assertEqual(response.status_code, 200)
        return {item['device_access_id']: item['access'] for item in response.json()['mappings']}

    def test_create_local_mapping_generates_id_and_defaults_to_member_auto(self):
        response = self.client.post(reverse('mapping_create'), {
            'first_name': 'Local', 'last_name': 'Member', 'classification': 'member',
            'access_override': 'auto',
        })
        mapping = AccountMapping.objects.get(is_manual=True)
        self.assertRedirects(response, reverse('account_mapping_detail', args=[mapping.pk]))
        self.assertRegex(mapping.account_user_id, r'^local-[0-9a-f]{32}$')
        self.assertEqual(mapping.accounting_system, 'local')
        self.assertEqual(mapping.classification, 'member')
        self.assertEqual(mapping.access_override, 'auto')
        self.assertIsNone(mapping.usd_balance)
        self.assertContains(self.client.get(reverse('mapping_create')), 'value="member" selected')

    def test_create_requires_name_and_valid_classification(self):
        for data in (
            {'classification': 'member', 'access_override': 'auto'},
            {'first_name': 'Bad', 'classification': 'invalid', 'access_override': 'auto'},
        ):
            response = self.client.post(reverse('mapping_create'), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(AccountMapping.objects.filter(is_manual=True).exists())

    def test_local_mapping_and_ids_survive_external_collision(self):
        self.client.post(reverse('mapping_create'), {
            'narration': 'Local Staff', 'classification': 'staff', 'access_override': 'reject',
            'usd_balance': '42.00', 'zwg_balance': '99.00',
        })
        local = AccountMapping.objects.get(is_manual=True)
        self.connect(mapping=local)
        self.sync([
            self.external_user(account=local.account_user_id, card='collision-card'),
            self.external_user(account='another-user'),
        ])
        local.refresh_from_db()
        self.assertEqual(local.narration, 'Local Staff')
        self.assertEqual(local.classification, 'staff')
        self.assertEqual(local.access_override, 'reject')
        self.assertEqual(local.usd_balance, 42)
        self.assertEqual(AccessId.objects.get(access_id='manual-card').mapping, local)
        self.assertFalse(AccessId.objects.filter(access_id='collision-card').exists())

    def test_staff_classification_grants_auto_but_override_wins_live_and_cached(self):
        self.config.authorization_flow = 'reject_all'
        self.config.save()
        self.connect()
        for classification, override, expected in (
            ('member', 'auto', 'REJECT'), ('staff', 'auto', 'GRANT'),
            ('staff', 'reject', 'REJECT'), ('staff', 'grant', 'GRANT'),
        ):
            with self.subTest(classification=classification, override=override):
                self.mapping.classification = classification
                self.mapping.access_override = override
                self.mapping.save()
                self.assertEqual(self.event('manual-card'), expected)
                self.assertEqual(self.cached()['manual-card'], expected)

    def test_external_classification_is_editable_and_preserved_by_sync(self):
        response = self.client.post(reverse('mapping_edit', args=[self.mapping.pk]), {
            'classification': 'staff', 'narration': 'Forged', 'is_manual': True,
        })
        self.assertEqual(response.status_code, 302)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.classification, 'staff')
        self.assertEqual(self.mapping.narration, 'External Member')
        self.assertFalse(self.mapping.is_manual)
        self.sync([self.external_user()])
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.classification, 'staff')
        self.assertEqual(self.mapping.narration, 'SYNCED NAME')

    def test_edit_local_mapping_keeps_identifier_and_local_ownership(self):
        self.mapping.is_manual = True
        self.mapping.save()
        self.client.post(reverse('mapping_edit', args=[self.mapping.pk]), {
            'first_name': 'Updated', 'classification': 'staff', 'access_override': 'grant',
            'account_user_id': 'forged-external-id', 'is_manual': False,
        })
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.account_user_id, 'external-100')
        self.assertTrue(self.mapping.is_manual)
        self.assertEqual(self.mapping.first_name, 'Updated')
        self.assertEqual(self.mapping.classification, 'staff')

    def test_connect_all_auth_types_with_device_scope_and_sync_protection(self):
        for index, auth_type in enumerate(('card', 'biometric', 'pin', 'other')):
            with self.subTest(auth_type=auth_type):
                access_id = f'manual-{index}'
                response = self.connect(access_id, self.device, auth_type=auth_type)
                self.assertEqual(response.status_code, 302)
                entry = AccessId.objects.get(access_id=access_id)
                self.assertTrue(entry.is_manual)
                self.assertTrue(entry.is_enabled)
                self.assertEqual(entry.access_device, self.device)
                self.sync([self.external_user(card=access_id)])
                entry.refresh_from_db()
                self.assertEqual(entry.auth_type, auth_type)
                self.assertEqual(entry.access_device, self.device)
                self.assertEqual(entry.mapping, self.mapping)
                self.assertEqual(self.event(access_id), 'GRANT')
                self.assertEqual(self.cached()[access_id], 'GRANT')

    def test_edit_external_id_preserves_old_disconnection_and_new_connection(self):
        entry = AccessId.objects.create(mapping=self.mapping, access_id='old-card', auth_type='card')
        url = reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk])
        response = self.client.post(url, {
            'access_id': 'new-card', 'auth_type': 'biometric', 'access_device': self.device.pk,
        })
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertTrue(entry.is_manual)
        self.assertFalse(entry.is_enabled)
        new = AccessId.objects.get(access_id='new-card')
        self.assertTrue(new.is_manual)
        self.assertEqual(new.auth_type, 'biometric')
        self.sync([self.external_user(card='old-card')])
        self.assertEqual(self.event('old-card'), 'REJECT')
        self.assertEqual(self.cached()['old-card'], 'REJECT')
        self.assertEqual(self.event('new-card'), 'GRANT')
        self.assertEqual(self.cached()['new-card'], 'GRANT')

    def test_edit_auth_type_in_place_takes_manual_ownership(self):
        entry = AccessId.objects.create(mapping=self.mapping, access_id='card', auth_type='card')
        self.client.post(reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk]), {
            'access_id': 'card', 'auth_type': 'pin', 'access_device': '',
        })
        entry.refresh_from_db()
        self.assertTrue(entry.is_manual)
        self.assertEqual(entry.auth_type, 'pin')
        self.assertEqual(AccessId.objects.filter(access_id='card').count(), 1)

    def test_disconnect_rejects_even_staff_and_manual_grants_after_repeated_syncs(self):
        self.mapping.classification = 'staff'
        self.mapping.access_override = 'grant'
        self.mapping.save()
        views.STAFF_USER_IDS = ['manual-card']
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        self.event('manual-card')
        response = self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        self.assertEqual(response.status_code, 302)
        for _ in range(2):
            self.sync([self.external_user()])
            entry.refresh_from_db()
            self.assertTrue(entry.is_manual)
            self.assertFalse(entry.is_enabled)
            self.assertEqual(self.event('manual-card'), 'REJECT')
            self.assertEqual(self.cached()['manual-card'], 'REJECT')
        detail = self.client.get(reverse('account_mapping_detail', args=[self.mapping.pk]))
        self.assertFalse(detail.context['connected'])
        self.assertEqual(len(detail.context['access_ids']), 0)
        self.assertContains(detail, 'Protected disconnections')
        self.assertTrue(AccessEventLog.objects.filter(user=self.mapping, access_status='grant').exists())

    def test_explicit_reconnect_reuses_protected_row(self):
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        response = self.connect()
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertTrue(entry.is_enabled)
        self.assertTrue(entry.is_manual)
        self.assertEqual(AccessId.objects.filter(access_id='manual-card').count(), 1)

    def test_duplicate_and_cross_mapping_connections_are_not_reassigned(self):
        other = AccountMapping.objects.create(account_user_id='other')
        self.connect()
        for mapping in (self.mapping, other):
            with self.subTest(mapping=mapping.pk):
                response = self.connect(mapping=mapping)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
        entry = AccessId.objects.get(access_id='manual-card')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        self.assertEqual(self.connect(mapping=other).status_code, 302)
        self.assertEqual(AccessId.objects.get(pk=entry.pk).mapping, other)

    def test_existing_unassigned_id_can_be_connected_manually(self):
        entry = AccessId.objects.create(access_id='unassigned', auth_type='card')
        response = self.connect('unassigned')
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.mapping, self.mapping)
        self.assertTrue(entry.is_manual)

    def test_scope_validation_allows_separate_devices_but_not_shared_shadowing(self):
        second = AccessControlDevice.objects.create(device_id='second-reader')
        self.assertEqual(self.connect('scoped', self.device).status_code, 302)
        self.assertEqual(self.connect('scoped', second).status_code, 302)
        self.assertTrue(self.connect('scoped').context['form'].errors)
        self.connect('shared')
        self.assertTrue(self.connect('shared', self.device).context['form'].errors)

    def test_disabled_device_scope_does_not_reject_another_device(self):
        second = AccessControlDevice.objects.create(device_id='second-reader')
        self.connect('scoped', self.device, auth_type='biometric')
        self.connect('scoped', second, auth_type='biometric')
        entry = AccessId.objects.get(access_id='scoped', access_device=self.device)
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        self.assertEqual(self.event('scoped'), 'REJECT')
        response = self.client.post(
            reverse('access_event', args=[second.device_id]), json.dumps({'access_id': 'scoped'}),
            content_type='application/json',
        )
        self.assertEqual(response.json()['access'], 'GRANT')

    def test_manual_device_specific_card_is_not_shared_with_other_devices(self):
        second = AccessControlDevice.objects.create(device_id='second-reader')
        self.connect('private-card', self.device)
        self.assertEqual(self.event('private-card'), 'GRANT')
        response = self.client.post(
            reverse('access_event', args=[second.device_id]),
            json.dumps({'access_id': 'private-card'}), content_type='application/json',
        )
        self.assertEqual(response.json()['access'], 'REJECT')
        cache = self.client.get(reverse('access_event', args=[second.device_id])).json()['mappings']
        self.assertNotIn('private-card', {item['device_access_id'] for item in cache})
        unknown = self.client.post(
            reverse('access_event', args=['unknown-device']),
            json.dumps({'access_id': 'private-card'}), content_type='application/json',
        )
        self.assertEqual(unknown.json()['access'], 'REJECT')

    def test_promoting_device_specific_id_to_shared_does_not_leave_a_local_reject(self):
        self.connect('promote', self.device)
        entry = AccessId.objects.get(access_id='promote')
        response = self.client.post(reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk]), {
            'access_id': 'promote', 'auth_type': 'card', 'access_device': '',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.event('promote'), 'GRANT')
        self.assertEqual(self.cached()['promote'], 'GRANT')
        self.assertEqual(AccessId.objects.filter(access_id='promote').count(), 1)
        shared = AccessId.objects.get(access_id='promote')
        self.assertIsNone(shared.access_device)
        self.assertTrue(shared.is_manual)

    def test_narrowing_shared_id_to_device_retains_rejection_outside_scope(self):
        self.connect('narrow')
        entry = AccessId.objects.get(access_id='narrow')
        response = self.client.post(reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk]), {
            'access_id': 'narrow', 'auth_type': 'card', 'access_device': self.device.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.event('narrow'), 'GRANT')
        self.assertEqual(self.cached()['narrow'], 'GRANT')
        second = AccessControlDevice.objects.create(device_id='other-reader')
        cache = self.client.get(reverse('access_event', args=[second.device_id])).json()['mappings']
        self.assertEqual(next(item['access'] for item in cache if item['device_access_id'] == 'narrow'), 'REJECT')
        scoped = AccessId.objects.get(access_id='narrow', is_enabled=True)
        response = self.client.post(reverse('mapping_access_id_edit', args=[self.mapping.pk, scoped.pk]), {
            'access_id': 'narrow', 'auth_type': 'pin', 'access_device': self.device.pk,
        })
        self.assertEqual(response.status_code, 302)
        scoped.refresh_from_db()
        self.assertEqual(scoped.auth_type, 'pin')

    def test_shared_reconnection_supersedes_old_device_disconnections(self):
        self.connect('reconnect', self.device)
        entry = AccessId.objects.get(access_id='reconnect')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        response = self.connect('reconnect')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.event('reconnect'), 'GRANT')
        self.assertEqual(self.cached()['reconnect'], 'GRANT')
        self.assertEqual(AccessId.objects.filter(access_id='reconnect').count(), 1)

    def test_one_step_mapping_cannot_restore_disconnected_id(self):
        self.connect('100')
        self.connect('200', self.device, auth_type='biometric')
        entry = AccessId.objects.get(access_id='200')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        self.assertEqual(self.event('CARD-100-WIEGAND-200'), 'REJECT')
        entry.refresh_from_db()
        self.assertFalse(entry.is_enabled)

    def test_one_step_mapping_cannot_use_disconnected_card(self):
        self.connect('100')
        entry = AccessId.objects.get(access_id='100')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        self.assertEqual(self.event('CARD-100-WIEGAND-300'), 'REJECT')
        self.assertFalse(AccessId.objects.filter(access_id='300').exists())

    def test_invalid_access_forms_do_not_write(self):
        for data in (
            {'access_id': ' ', 'auth_type': 'card'},
            {'access_id': 'id', 'auth_type': 'invalid'},
            {'access_id': 'id', 'auth_type': 'card', 'access_device': 999999},
        ):
            response = self.client.post(reverse('mapping_access_id_create', args=[self.mapping.pk]), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(AccessId.objects.exists())

    def test_new_management_endpoints_require_staff_and_csrf(self):
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        urls = [
            reverse('mapping_create'), reverse('mapping_edit', args=[self.mapping.pk]),
            reverse('mapping_access_id_create', args=[self.mapping.pk]),
            reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk]),
            reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]),
        ]
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        for url in urls:
            self.assertEqual(csrf_client.post(url, {}).status_code, 403)
        user = get_user_model().objects.create_user(username='viewer')
        self.client.force_login(user)
        for url in urls:
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.client.logout()
        for url in urls:
            self.assertEqual(self.client.post(url, {}).status_code, 302)

    def test_remove_is_post_only_and_cross_mapping_ids_return_404(self):
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        self.assertEqual(self.client.get(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk])).status_code, 405)
        other = AccountMapping.objects.create(account_user_id='other')
        for route in ('mapping_access_id_edit', 'mapping_access_id_remove'):
            url = reverse(route, args=[other.pk, entry.pk])
            self.assertEqual(self.client.post(url, {}).status_code, 404)
        self.assertEqual(self.client.get(reverse('mapping_access_id_create', args=[99999])).status_code, 404)

    def test_cache_failure_rolls_back_access_edit_and_disconnection(self):
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        with patch.object(views, 'get_access_decisions_list', side_effect=RuntimeError('cache unavailable')):
            with self.assertRaisesMessage(RuntimeError, 'cache unavailable'):
                self.client.post(reverse('mapping_access_id_edit', args=[self.mapping.pk, entry.pk]), {
                    'access_id': 'new-id', 'auth_type': 'pin',
                })
            with self.assertRaisesMessage(RuntimeError, 'cache unavailable'):
                self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        entry.refresh_from_db()
        self.assertTrue(entry.is_enabled)
        self.assertFalse(AccessId.objects.filter(access_id='new-id').exists())

    def test_database_rejects_duplicate_shared_ids(self):
        AccessId.objects.create(access_id='shared', mapping=self.mapping)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccessId.objects.create(access_id='shared', mapping=self.mapping)

    def test_filters_and_counts_ignore_disconnected_ids_and_filter_classification(self):
        self.connect()
        entry = AccessId.objects.get(access_id='manual-card')
        self.client.post(reverse('mapping_access_id_remove', args=[self.mapping.pk, entry.pk]))
        AccessId.objects.create(access_id='unmapped')
        response = self.client.get('/', {'connection_status': 'not_connected'})
        self.assertEqual(response.context['connected_access_ids'], 0)
        self.assertEqual(response.context['not_connected_users'], 1)
        self.assertEqual(len(response.context['users']), 1)
        self.assertEqual(len(self.client.get('/', {'device_access_id': 'manual-card'}).context['users']), 0)
        self.assertEqual(len(self.client.get('/', {'search': 'manual-card'}).context['users']), 0)
        self.mapping.classification = 'staff'
        self.mapping.save()
        self.assertEqual(len(self.client.get('/', {'classification': 'member'}).context['users']), 0)
        self.assertEqual(len(self.client.get('/', {'classification': 'staff'}).context['users']), 1)

    def test_automatic_sync_still_updates_unprotected_users_and_ids(self):
        self.sync([self.external_user(card='external-card')])
        self.mapping.refresh_from_db()
        entry = AccessId.objects.get(access_id='external-card')
        self.assertFalse(entry.is_manual)
        self.assertEqual(entry.mapping, self.mapping)
        self.assertEqual(self.mapping.usd_balance, 20)
        self.assertEqual(self.mapping.classification, 'member')

    def test_external_sync_accepts_zero_card_id_and_skips_missing_ids(self):
        self.sync([self.external_user(card=0), self.external_user(account='no-card', card=None)])
        self.assertTrue(AccessId.objects.filter(access_id='0').exists())
        self.assertFalse(AccessId.objects.filter(access_id='None').exists())

    def test_legacy_manual_connection_requires_csrf_and_marks_ownership(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        payload = json.dumps({'account_user_id': self.mapping.account_user_id, 'device_access_id': 'legacy-card'})
        self.assertEqual(client.post('/api/mappings', payload, content_type='application/json').status_code, 403)
        client.get(reverse('account_mapping_detail', args=[self.mapping.pk]))
        token = client.cookies['csrftoken'].value
        response = client.post('/api/mappings', payload, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201)
        entry = AccessId.objects.get(access_id='legacy-card')
        self.assertTrue(entry.is_manual)
        response = client.delete(f'/api/mappings/{self.mapping.account_user_id}', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 204)
        entry.refresh_from_db()
        self.assertFalse(entry.is_enabled)
        self.sync([self.external_user(card='legacy-card')])
        entry.refresh_from_db()
        self.assertFalse(entry.is_enabled)

    def test_cleanup_preserves_local_mappings_and_mappings_with_manual_connections(self):
        local = AccountMapping.objects.create(account_user_id='local-only', is_manual=True)
        protected = self.mapping
        self.connect()
        stale = AccountMapping.objects.create(account_user_id='stale')
        response = Mock()
        response.json.return_value = [{'accountNumber': 'external-survivor'}]
        with patch('access.management.commands.cleanup_stale_account_mappings.requests.get', return_value=response):
            call_command('cleanup_stale_account_mappings', force=True, stdout=StringIO())
        self.assertTrue(AccountMapping.objects.filter(pk=local.pk).exists())
        self.assertTrue(AccountMapping.objects.filter(pk=protected.pk).exists())
        self.assertFalse(AccountMapping.objects.filter(pk=stale.pk).exists())

    def test_controls_visible_only_to_staff(self):
        self.connect()
        detail_url = reverse('account_mapping_detail', args=[self.mapping.pk])
        for url, text in [('/', 'Create local mapping'), (detail_url, 'Connect access ID')]:
            self.assertContains(self.client.get(url), text)
        user = get_user_model().objects.create_user(username='read-only')
        self.client.force_login(user)
        for url, text in [('/', 'Create local mapping'), (detail_url, 'Connect access ID')]:
            self.assertNotContains(self.client.get(url), text)
