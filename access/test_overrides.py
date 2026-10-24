import json
from unittest.mock import Mock, mock_open, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from . import views
from .models import AccessControlDevice, AccessEventLog, AccessId, AccountMapping, Setting, SystemMetadata


class MappingAccessOverrideTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username='override-admin', is_staff=True)
        self.client.force_login(self.staff)
        self.mapping = AccountMapping.objects.create(
            account_user_id='override-100', narration='Override Member', usd_balance='10.00',
        )
        self.device = AccessControlDevice.objects.create(device_id='override-reader')
        self.card = AccessId.objects.create(mapping=self.mapping, access_id='100', auth_type='card')
        self.biometric = AccessId.objects.create(
            mapping=self.mapping, access_id='200', auth_type='biometric', access_device=self.device,
        )
        self.settings_obj = Setting.get_solo()
        self.settings_obj.authorization_flow = 'grant_all'
        self.settings_obj.save()
        self.url = reverse('account_mapping_override', args=[self.mapping.pk])
        self.device_url = reverse('access_event', args=[self.device.device_id])
        self.addCleanup(patch.stopall)
        patch.object(views, 'access_decisions_list', []).start()
        patch.object(views, 'STAFF_USER_IDS', []).start()

    def set_override(self, value):
        self.mapping.access_override = value
        self.mapping.save(update_fields=['access_override'])

    def post_event(self, access_id):
        return self.client.post(
            self.device_url, json.dumps({'access_id': access_id}), content_type='application/json',
        )

    def cached_decisions(self):
        response = self.client.get(self.device_url)
        self.assertEqual(response.status_code, 200)
        decisions = response.json()['mappings']
        self.assertTrue(all(set(item) == {'device_access_id', 'access'} for item in decisions))
        self.assertEqual(len(decisions), len({item['device_access_id'] for item in decisions}))
        return {item['device_access_id']: item['access'] for item in decisions}

    def test_default_auto_preserves_automatic_decisions(self):
        self.assertEqual(self.mapping.access_override, 'auto')
        for flow, expected in [('grant_all', 'GRANT'), ('reject_all', 'REJECT')]:
            with self.subTest(flow=flow):
                self.settings_obj.authorization_flow = flow
                self.settings_obj.save()
                self.assertEqual(self.post_event('100').json(), {'access': expected})
                self.assertEqual(self.cached_decisions()['100'], expected)
                self.assertEqual(AccessEventLog.objects.latest('pk').decision_type, 'auto')

    def test_manual_overrides_win_for_live_cached_and_stored_decisions(self):
        for override, flow in [('reject', 'grant_all'), ('grant', 'reject_all')]:
            with self.subTest(override=override):
                self.settings_obj.authorization_flow = flow
                self.settings_obj.save()
                self.set_override(override)
                # Simulate a stale grant held by a different worker.
                views.access_decisions_list = [{'card': [{'device_access_id': '100', 'access': 'GRANT'}]}]
                for access_id in ('100', '200'):
                    response = self.post_event(access_id)
                    self.assertEqual(response.json(), {'access': override.upper()})
                    event = AccessEventLog.objects.latest('pk')
                    self.assertEqual(event.decision_type, 'manual')
                    self.assertEqual(event.user, self.mapping)
                self.assertEqual(self.cached_decisions(), {'100': override.upper(), '200': override.upper()})
                persisted = SystemMetadata.objects.get().access_decision_cache
                self.assertEqual(persisted[1]['card'][0]['access'], override.upper())

    def test_manual_override_does_not_call_balance_fetch(self):
        self.settings_obj.authorization_flow = 'check_usd_balance'
        self.mapping.usd_balance = -100
        self.mapping.access_override = 'grant'
        self.mapping.save()
        with patch.object(views, 'get_accounting_balances') as fetch:
            self.assertEqual(views.get_access_decision('100', self.mapping, self.settings_obj), 'GRANT')
        fetch.assert_not_called()

    def test_auto_balance_rules_still_apply_to_live_and_cached_decisions(self):
        for flow in ('check_usd_balance', 'check_zwg_balance', 'check_usd_or_zwg_balance'):
            for balance, expected in [('10.00', 'GRANT'), ('-10.00', 'REJECT')]:
                with self.subTest(flow=flow, balance=balance):
                    self.settings_obj.authorization_flow = flow
                    self.settings_obj.save()
                    self.mapping.usd_balance = balance
                    self.mapping.zwg_balance = balance
                    self.mapping.save()
                    self.assertEqual(self.post_event('100').json(), {'access': expected})
                    self.assertEqual(self.cached_decisions()['100'], expected)

    def test_staff_exception_cannot_bypass_manual_reject(self):
        views.STAFF_USER_IDS = ['100', '200', 'unmapped-staff']
        self.set_override('reject')
        for access_id in ('100', '200'):
            self.assertEqual(self.post_event(access_id).json(), {'access': 'REJECT'})
        self.assertEqual(self.post_event('unmapped-staff').json(), {'access': 'GRANT'})
        self.assertEqual(self.cached_decisions(), {'100': 'REJECT', '200': 'REJECT', 'unmapped-staff': 'GRANT'})

    def test_one_step_mapping_honors_override_and_keeps_auto_behavior(self):
        for override, expected in [('reject', 'REJECT'), ('grant', 'GRANT'), ('auto', 'GRANT')]:
            with self.subTest(override=override):
                self.set_override(override)
                response = self.post_event('CARD-100-WIEGAND-300')
                self.assertEqual(response.json(), {'access': expected})
                event = AccessEventLog.objects.latest('pk')
                self.assertEqual(event.access_type, 'one_step_mapping')
                self.assertEqual(event.access_status, expected.lower())
                self.assertEqual(event.decision_type, 'auto' if override == 'auto' else 'manual')

    def test_inactive_device_still_rejects_manual_grant(self):
        self.set_override('grant')
        self.device.is_active = False
        self.device.save()
        self.assertEqual(self.post_event('100').json(), {'access': 'REJECT'})

    def test_unmapped_id_remains_rejected(self):
        self.assertEqual(self.post_event('unknown').json(), {'access': 'REJECT'})
        AccessId.objects.create(access_id='unmapped-card', auth_type='card')
        self.assertEqual(self.cached_decisions()['unmapped-card'], 'REJECT')

    def test_all_linked_auth_types_receive_override_in_cache(self):
        self.set_override('reject')
        for index, auth_type in enumerate(('pin', 'other')):
            AccessId.objects.create(
                mapping=self.mapping, access_id=f'local-{index}', auth_type=auth_type, access_device=self.device,
            )
        AccessId.objects.create(mapping=self.mapping, access_id='global-pin', auth_type='pin')
        decisions = self.cached_decisions()
        for access_id in ('100', '200', 'local-0', 'local-1', 'global-pin'):
            self.assertEqual(decisions[access_id], 'REJECT')

    def test_device_scoped_ids_use_the_correct_mapping(self):
        other_device = AccessControlDevice.objects.create(device_id='other-reader')
        other_mapping = AccountMapping.objects.create(account_user_id='other', access_override='reject')
        AccessId.objects.create(
            mapping=other_mapping, access_id='200', auth_type='biometric', access_device=other_device,
        )
        self.set_override('grant')
        self.assertEqual(self.post_event('200').json(), {'access': 'GRANT'})
        response = self.client.post(
            reverse('access_event', args=[other_device.device_id]),
            json.dumps({'access_id': '200'}), content_type='application/json',
        )
        self.assertEqual(response.json(), {'access': 'REJECT'})
        self.assertEqual(self.cached_decisions()['200'], 'GRANT')
        other_cache = self.client.get(reverse('access_event', args=[other_device.device_id])).json()['mappings']
        self.assertEqual(next(item['access'] for item in other_cache if item['device_access_id'] == '200'), 'REJECT')

    def test_device_scoped_cards_override_global_duplicate_entries(self):
        other_device = AccessControlDevice.objects.create(device_id='other-card-reader')
        other_mapping = AccountMapping.objects.create(account_user_id='other-card', access_override='reject')
        self.card.access_device = self.device
        self.card.save()
        AccessId.objects.create(
            mapping=other_mapping, access_id='100', auth_type='card', access_device=other_device,
        )
        self.set_override('grant')
        self.assertEqual(self.post_event('100').json(), {'access': 'GRANT'})
        self.assertEqual(self.cached_decisions()['100'], 'GRANT')
        response = self.client.get(reverse('access_event', args=[other_device.device_id]))
        decisions = response.json()['mappings']
        self.assertEqual(next(item['access'] for item in decisions if item['device_access_id'] == '100'), 'REJECT')

    def test_staff_can_set_override_and_restore_auto_with_stale_worker_cache(self):
        for value, expected in [('reject', 'REJECT'), ('grant', 'GRANT'), ('auto', 'GRANT')]:
            with self.subTest(value=value):
                response = self.client.post(self.url, {'access_override': value})
                self.assertRedirects(response, reverse('account_mapping_detail', args=[self.mapping.pk]))
                self.mapping.refresh_from_db()
                self.assertEqual(self.mapping.access_override, value)
                views.access_decisions_list = [{'card': [{'device_access_id': '100', 'access': 'REJECT'}]}]
                self.assertEqual(self.cached_decisions()['100'], expected)
                self.assertEqual(self.post_event('100').json(), {'access': expected})

    def test_reset_auto_removes_previous_manual_grant(self):
        self.settings_obj.authorization_flow = 'reject_all'
        self.settings_obj.save()
        self.client.post(self.url, {'access_override': 'grant'})
        stale = views.access_decisions_list
        self.client.post(self.url, {'access_override': 'auto'})
        views.access_decisions_list = stale
        self.assertEqual(self.cached_decisions()['100'], 'REJECT')
        self.assertEqual(self.post_event('100').json(), {'access': 'REJECT'})

    def test_override_endpoint_permissions_and_methods(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'access_override': 'grant'}).status_code, 302)
        user = get_user_model().objects.create_user(username='regular-operator')
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, {'access_override': 'grant'}).status_code, 403)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'auto')
        user.is_superuser = True
        user.save()
        self.assertEqual(self.client.post(self.url, {'access_override': 'reject'}).status_code, 302)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'reject')

    def test_override_endpoint_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {'access_override': 'grant'}).status_code, 403)

    def test_invalid_override_is_rejected_without_changes(self):
        for value in ('', 'GRANT', 'invalid', None):
            with self.subTest(value=value):
                data = {'access_override': value} if value is not None else {}
                self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'auto')
        missing = reverse('account_mapping_override', args=[self.mapping.pk + 100])
        self.assertEqual(self.client.post(missing, {'access_override': 'grant'}).status_code, 404)

    def test_override_and_cache_update_roll_back_together_on_failure(self):
        with patch.object(views, 'get_access_decisions_list', side_effect=RuntimeError('cache failure')):
            with self.assertRaisesMessage(RuntimeError, 'cache failure'):
                self.client.post(self.url, {'access_override': 'grant'})
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'auto')

    def test_quick_action_preserves_list_filters_and_page(self):
        response = self.client.post(self.url, {
            'access_override': 'reject', 'return_to': 'list', 'list_query': 'search=Override&page=2',
        })
        self.assertEqual(response.url, '/?search=Override&page=2')

    def test_controls_and_decision_types_on_both_pages(self):
        SystemMetadata.objects.create()
        self.set_override('reject')
        for url in (reverse('account_mapping_list'), reverse('account_mapping_detail', args=[self.mapping.pk])):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'Manual: Reject')
                self.assertContains(response, f'action="{self.url}"')
                self.assertContains(response, 'access_override.js')
                self.assertContains(response, 'value="auto"')
                self.assertContains(response, 'value="grant"')
                self.assertContains(response, 'value="reject"')
        self.staff.is_staff = False
        self.staff.save()
        for url in (reverse('account_mapping_list'), reverse('account_mapping_detail', args=[self.mapping.pk])):
            response = self.client.get(url)
            self.assertContains(response, 'Manual: Reject')
            self.assertNotContains(response, f'action="{self.url}"')

    def test_event_decision_type_is_a_historical_snapshot(self):
        self.set_override('reject')
        self.post_event('100')
        self.set_override('auto')
        event = AccessEventLog.objects.get()
        self.assertEqual(event.decision_type, 'manual')
        response = self.client.get(reverse('account_mapping_detail', args=[self.mapping.pk]))
        self.assertContains(response, '<td>Manual</td>', html=True)

    def test_external_sync_preserves_override_and_rebuilds_decisions(self):
        self.set_override('reject')
        response = Mock()
        response.json.return_value = [
            {
                'accountNumber': self.mapping.account_user_id, 'firstName': 'Synced',
                'lastName': 'Member', 'memberName': 'Synced Member', 'usdBalance': 999,
                'zwdBalance': 10, 'cardId': '100', 'access_override': 'grant',
            },
            {'accountNumber': 'new-user', 'memberName': 'New Member', 'cardId': 'new-card'},
        ]
        with patch.object(views.requests, 'get', return_value=response), patch('access.views.open', mock_open()):
            result = views.sync_external_users()
        self.assertTrue(result['ok'])
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'reject')
        self.assertEqual(self.mapping.first_name, 'Synced')
        self.assertEqual(self.mapping.usd_balance, 999)
        self.assertEqual(AccountMapping.objects.get(account_user_id='new-user').access_override, 'auto')
        self.assertEqual(self.cached_decisions()['100'], 'REJECT')
        self.assertEqual(self.post_event('100').json(), {'access': 'REJECT'})
