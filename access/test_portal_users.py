import json
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from . import views
from .models import AccessControlDevice, AccessId, AccountMapping, MappingModalState, Setting
from .roles import ROLE_GROUPS, get_assigned_role, get_portal_role


class PortalTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user('portal-admin')
        cls.admin.groups.add(Group.objects.get(name=ROLE_GROUPS['admin']))
        cls.staff = User.objects.create_user('portal-staff')
        cls.staff.groups.add(Group.objects.get(name=ROLE_GROUPS['staff']))
        cls.viewer = User.objects.create_user('portal-viewer')
        cls.superuser = User.objects.create_superuser('root', password='RootPassword!8791')
        cls.mapping = AccountMapping.objects.create(account_user_id='portal-member', narration='Member')

    def setUp(self):
        self.client.force_login(self.admin)

    def user_data(self, **changes):
        data = {
            'username': 'new-operator', 'first_name': 'New', 'last_name': 'Operator',
            'email': 'operator@example.com', 'role': 'staff', 'is_active': 'on',
            'password1': 'Unique-Portal-Password!592', 'password2': 'Unique-Portal-Password!592',
        }
        data.update(changes)
        return data

    def edit_data(self, user, **changes):
        return self.user_data(
            username=user.username, password1='', password2='',
            **changes,
        )


class PortalUserManagementTests(PortalTestCase):
    def test_pages_require_login(self):
        self.client.logout()
        for name, args in (
            ('portal_user_list', []), ('portal_user_create', []),
            ('portal_user_edit', [self.viewer.pk]),
        ):
            url = reverse(name, args=args)
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_non_admins_cannot_read_or_write_user_management(self):
        for actor in (self.viewer, self.staff):
            self.client.force_login(actor)
            for url in (
                reverse('portal_user_list'), reverse('portal_user_create'),
                reverse('portal_user_edit', args=[self.viewer.pk]),
            ):
                with self.subTest(actor=actor.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    expected = 405 if url == reverse('portal_user_list') else 403
                    self.assertEqual(self.client.post(url, self.user_data()).status_code, expected)
        self.assertFalse(get_user_model().objects.filter(username='new-operator').exists())

    def test_admin_can_create_each_role_without_django_admin_privileges(self):
        for role in ROLE_GROUPS:
            with self.subTest(role=role):
                response = self.client.post(
                    reverse('portal_user_create'), self.user_data(username=f'new-{role}', role=role),
                )
                self.assertRedirects(response, reverse('portal_user_list'))
                user = get_user_model().objects.get(username=f'new-{role}')
                self.assertEqual(get_portal_role(user), role)
                self.assertTrue(user.check_password('Unique-Portal-Password!592'))
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)

    def test_validation_errors_do_not_create_accounts(self):
        for changes, field in (
            ({'password1': '', 'password2': ''}, 'password1'),
            ({'password2': 'different'}, 'password2'),
            ({'password1': '123', 'password2': '123'}, 'password1'),
            ({'role': 'superuser'}, 'role'),
            ({'username': self.viewer.username}, 'username'),
            ({'email': 'not-an-email'}, 'email'),
        ):
            with self.subTest(changes=changes):
                response = self.client.post(reverse('portal_user_create'), self.user_data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['form'].errors)
        self.assertFalse(get_user_model().objects.filter(username='new-operator').exists())

    def test_password_similarity_checks_updated_user_details(self):
        password = 'NewOperatorVeryLongName'
        response = self.client.post(reverse('portal_user_create'), self.user_data(
            username=password, password1=password, password2=password,
        ))
        self.assertIn('password1', response.context['form'].errors)

    def test_role_edit_preserves_other_groups_and_password(self):
        other_group = Group.objects.create(name='Other business group')
        self.staff.groups.add(other_group, Group.objects.get(name=ROLE_GROUPS['viewer']))
        self.staff.set_password('KeepThisPassword!781')
        self.staff.save()
        response = self.client.post(
            reverse('portal_user_edit', args=[self.staff.pk]),
            self.edit_data(self.staff, role='viewer'),
        )
        self.assertRedirects(response, reverse('portal_user_list'))
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password('KeepThisPassword!781'))
        self.assertEqual(get_portal_role(self.staff), 'viewer')
        self.assertSetEqual(set(self.staff.groups.values_list('name', flat=True)),
                            {other_group.name, ROLE_GROUPS['viewer']})

    def test_legacy_staff_can_be_demoted(self):
        legacy = get_user_model().objects.create_user('legacy-operator', is_staff=True)
        self.client.post(reverse('portal_user_edit', args=[legacy.pk]),
                         self.edit_data(legacy, role='viewer'))
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_staff)
        self.assertEqual(get_portal_role(legacy), 'viewer')

    def test_editing_legacy_staff_preserves_existing_staff_flag(self):
        legacy = get_user_model().objects.create_user('legacy-operator', is_staff=True)
        self.client.post(reverse('portal_user_edit', args=[legacy.pk]),
                         self.edit_data(legacy, role='staff'))
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertEqual(get_portal_role(legacy), 'staff')

    def test_submitted_privilege_flags_are_ignored(self):
        response = self.client.post(reverse('portal_user_create'), self.user_data(
            role='viewer', is_staff='on', is_superuser='on',
        ))
        self.assertRedirects(response, reverse('portal_user_list'))
        user = get_user_model().objects.get(username='new-operator')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(get_portal_role(user), 'viewer')

    def test_cannot_deactivate_or_demote_self(self):
        for changes in ({'role': 'staff'}, {'role': 'viewer'}, {'is_active': ''}):
            with self.subTest(changes=changes):
                data = self.edit_data(self.admin, role='admin')
                data.update(changes)
                response = self.client.post(reverse('portal_user_edit', args=[self.admin.pk]), data)
                self.assertTrue(response.context['form'].non_field_errors())
                self.admin.refresh_from_db()
                self.assertTrue(self.admin.is_active)
                self.assertEqual(get_portal_role(self.admin), 'admin')

    def test_portal_admin_cannot_edit_superuser(self):
        url = reverse('portal_user_edit', args=[self.superuser.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.edit_data(self.superuser)).status_code, 403)

    def test_superuser_cannot_be_demoted(self):
        self.client.force_login(self.superuser)
        other_root = get_user_model().objects.create_superuser('other-root', password='OtherRoot!982')
        response = self.client.post(reverse('portal_user_edit', args=[other_root.pk]),
                                    self.edit_data(other_root, role='staff'))
        self.assertIn('role', response.context['form'].errors)
        other_root.refresh_from_db()
        self.assertTrue(other_root.is_superuser)

    def test_self_password_reset_keeps_session_and_changes_password(self):
        data = self.edit_data(self.admin, role='admin')
        data.update(password1='Changed-Password!482', password2='Changed-Password!482')
        response = self.client.post(reverse('portal_user_edit', args=[self.admin.pk]), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Portal user updated.')
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password('Changed-Password!482'))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)

    def test_password_reset_invalidates_other_sessions(self):
        operator = Client()
        operator.force_login(self.staff)
        data = self.edit_data(self.staff)
        data.update(password1='Reset-Password!482', password2='Reset-Password!482')
        self.client.post(reverse('portal_user_edit', args=[self.staff.pk]), data)
        self.assertEqual(operator.get(reverse('account_mapping_list')).status_code, 302)
        self.assertTrue(operator.login(username=self.staff.username, password='Reset-Password!482'))

    def test_deactivation_and_reactivation_preserve_role(self):
        operator = Client()
        operator.force_login(self.staff)
        url = reverse('portal_user_edit', args=[self.staff.pk])
        self.client.post(url, self.edit_data(self.staff, is_active=''))
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertIsNone(get_portal_role(self.staff))
        self.assertEqual(operator.get(reverse('account_mapping_list')).status_code, 302)
        self.assertFalse(operator.login(username=self.staff.username, password='anything'))
        self.assertContains(self.client.get(url), 'value="staff" selected')
        self.assertContains(self.client.get(reverse('portal_user_list')), 'Inactive')
        self.client.post(url, self.edit_data(self.staff))
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)
        self.assertEqual(get_portal_role(self.staff), 'staff')

    def test_role_change_takes_effect_in_existing_session(self):
        operator = Client()
        operator.force_login(self.staff)
        self.assertEqual(operator.get(reverse('mapping_create')).status_code, 200)
        self.client.post(reverse('portal_user_edit', args=[self.staff.pk]),
                         self.edit_data(self.staff, role='viewer'))
        self.assertEqual(operator.get(reverse('mapping_create')).status_code, 403)

    def test_list_search_pagination_and_escaping(self):
        get_user_model().objects.create_user('escape-user', first_name='<script>alert(1)</script>')
        response = self.client.get(reverse('portal_user_list'), {'q': 'escape-user'})
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertNotContains(response, self.staff.username)
        get_user_model().objects.bulk_create([
            get_user_model()(username=f'extra-{index}') for index in range(26)
        ])
        response = self.client.get(reverse('portal_user_list'), {'q': 'extra-', 'page': 2})
        self.assertEqual(len(response.context['rows']), 1)
        self.assertContains(response, '?q=extra-&amp;page=1')

    def test_forms_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for url in (reverse('portal_user_create'), reverse('portal_user_edit', args=[self.staff.pk])):
            self.assertEqual(client.post(url, self.user_data()).status_code, 403)
        client.get(reverse('portal_user_create'))
        response = client.post(
            reverse('portal_user_create'), self.user_data(),
            HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value,
        )
        self.assertEqual(response.status_code, 302)

    def test_missing_account_returns_404(self):
        self.assertEqual(self.client.get(reverse('portal_user_edit', args=[999999])).status_code, 404)

    def test_role_precedence_and_existing_flags(self):
        self.assertIsNone(get_portal_role(AnonymousUser()))
        self.assertEqual(get_portal_role(self.viewer), 'viewer')
        legacy = get_user_model().objects.create_user('legacy', is_staff=True)
        self.assertEqual(get_portal_role(legacy), 'staff')
        legacy.groups.add(Group.objects.get(name=ROLE_GROUPS['admin']))
        self.assertEqual(get_portal_role(legacy), 'admin')
        legacy.is_active = False
        legacy.save()
        self.assertIsNone(get_portal_role(legacy))
        self.assertEqual(get_assigned_role(legacy), 'admin')
        self.assertEqual(get_portal_role(self.superuser), 'admin')

    def test_migration_assigns_existing_roles_and_preserves_groups_and_flags(self):
        legacy = get_user_model().objects.create_user('legacy', is_staff=True)
        unrelated = Group.objects.create(name='Existing group')
        legacy.groups.add(unrelated)
        seed_roles = import_module('access.migrations.0014_portal_roles').seed_roles
        seed_roles(apps, connection.schema_editor())
        seed_roles(apps, connection.schema_editor())
        for user, role in ((legacy, 'staff'), (self.viewer, 'viewer'), (self.superuser, 'admin')):
            self.assertTrue(user.groups.filter(name=ROLE_GROUPS[role]).exists())
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertTrue(legacy.groups.filter(pk=unrelated.pk).exists())


class PortalRoleEnforcementTests(PortalTestCase):
    def test_viewer_controls_are_hidden(self):
        self.client.force_login(self.viewer)
        for url in (reverse('account_mapping_list'),
                    reverse('account_mapping_detail', args=[self.mapping.pk])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            for text in ('Create local mapping', 'Connect access ID', 'Portal users', 'Settings'):
                self.assertNotContains(response, text)
            self.assertNotContains(response, 'access/js/app.js')

    def test_group_staff_sees_and_uses_mapping_controls(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('account_mapping_list'))
        self.assertContains(response, 'Create local mapping')
        self.assertContains(response, 'access/js/app.js')
        self.assertNotContains(response, 'Portal users')
        with patch.object(views, '_refresh_access_cache'):
            response = self.client.post(reverse('mapping_create'), {
                'narration': 'Local Person', 'classification': 'member', 'access_override': 'auto',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AccountMapping.objects.filter(narration='Local Person').exists())

    def test_viewer_cannot_mutate_mapping_surfaces(self):
        self.client.force_login(self.viewer)
        entry = AccessId.objects.create(mapping=self.mapping, access_id='viewer-test')
        for name, args in (
            ('mapping_create', []), ('mapping_edit', [self.mapping.pk]),
            ('mapping_access_id_create', [self.mapping.pk]),
            ('mapping_access_id_edit', [self.mapping.pk, entry.pk]),
            ('mapping_access_id_remove', [self.mapping.pk, entry.pk]),
            ('account_mapping_override', [self.mapping.pk]),
        ):
            with self.subTest(name=name):
                response = self.client.post(reverse(name, args=args), {'access_override': 'grant'})
                self.assertEqual(response.status_code, 403)
        self.mapping.refresh_from_db()
        self.assertEqual(self.mapping.access_override, 'auto')
        self.assertTrue(AccessId.objects.filter(pk=entry.pk).exists())

    def test_settings_is_admin_only_for_reads_and_writes(self):
        for user in (self.staff, self.viewer):
            self.client.force_login(user)
            for method in ('get', 'post'):
                self.assertEqual(getattr(self.client, method)(reverse('settings')).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('settings')).status_code, 200)
        with patch.object(views, 'get_access_decisions_list', return_value=[]):
            response = self.client.post(reverse('settings'), {'authorization_flow': 'reject_all'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Setting.get_solo().authorization_flow, 'reject_all')

    def test_browser_api_roles_cover_all_methods_and_sync(self):
        requests = [
            ('get', '/api/mappings/pending', {}),
            ('post', '/api/mappings', {}),
            ('delete', '/api/mappings/portal-member', {}),
            ('post', '/api/mappings/update', {}),
            ('post', '/api/modal-state', {'state': 'closed'}),
        ]
        for user, status in ((None, 302), (self.viewer, 403)):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for method, url, data in requests:
                with self.subTest(user=user, url=url, method=method):
                    response = getattr(self.client, method)(
                        url, json.dumps(data) if method != 'get' else {},
                        content_type='application/json',
                    )
                    self.assertEqual(response.status_code, status)
        self.client.force_login(self.staff)
        with patch.object(views, 'fetch_external_users') as sync:
            self.assertEqual(self.client.post('/api/mappings/update').status_code, 403)
            sync.assert_not_called()
        self.assertEqual(self.client.get('/api/mappings/pending').status_code, 200)
        self.assertEqual(self.client.post('/api/modal-state', json.dumps({'state': 'closed'}),
                                        content_type='application/json').status_code, 201)
        self.assertEqual(MappingModalState.objects.get(pk=1).state, 'closed')

    def test_admin_can_sync(self):
        with patch.object(views, 'sync_external_users', return_value={
            'ok': True, 'status': 'updated', 'updated_count': 0,
        }) as sync:
            response = self.client.post('/api/mappings/update')
        self.assertEqual(response.status_code, 200)
        sync.assert_called_once()

    def test_browser_api_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for method, url in (
            ('post', '/api/mappings'), ('delete', '/api/mappings/portal-member'),
            ('post', '/api/mappings/update'), ('post', '/api/modal-state'),
        ):
            self.assertEqual(getattr(client, method)(url, '{}', content_type='application/json').status_code, 403)
        client.get(reverse('account_mapping_list'))
        response = client.post('/api/modal-state', json.dumps({'state': 'closed'}),
                               content_type='application/json',
                               HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 201)

    def test_device_event_api_is_unchanged_and_does_not_require_portal_login(self):
        device = AccessControlDevice.objects.create(device_id='portal-role-reader')
        self.client.logout()
        response = self.client.post(
            reverse('access_event', args=[device.device_id]),
            json.dumps({'access_id': 'unknown-card'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'access': 'REJECT'})
