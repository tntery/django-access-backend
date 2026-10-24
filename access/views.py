import json
import sqlite3
import environ
import requests
import logging
from requests.exceptions import RequestException, HTTPError
from http import HTTPStatus
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_POST
from .filters import AccountMappingFilter
from .models import AccessControlDevice, AccessDeviceSyncLog, AccessEventLog, TamperEventLog, AccessId, AccountMapping, PendingAccountMapping, MappingModalState, Setting, SystemMetadata
from .forms import ManualAccessIdForm, ManualMappingForm, MappingClassificationForm, PortalUserForm, SettingForm
from .roles import get_assigned_role, require_role

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'
ACCOUNT_MAPPING_PAGE_SIZE = 25
MAPPING_EVENT_PAGE_SIZE = 25
logger = logging.getLogger(__name__)

env = environ.Env()
environ.Env.read_env()
EXTERNAL_ACCOUNTING_URL = env('ACCOUNTING_API_USERLIST_URL')
STAFF_USER_IDS = env.list('STAFF_USER_IDS', default=[])
DUMP_DATABASE_SECRET_KEY = env('DUMP_DATABASE_SECRET_KEY', default='replace_thesis_with_a_sekure_kiy')

# access_decisions_list = SystemMetadata.objects.first().access_decision_cache if SystemMetadata.objects.first() and SystemMetadata.objects.first().access_decision_cache else []
access_decisions_list = []

# print(f"Initial access_decisions_list loaded from cache: {access_decisions_list}")  # Debug log to verify initial loading of access decisions cache

class AccessLoginView(LoginView):
    template_name = 'access/login.html'
    redirect_authenticated_user = True

    def form_valid(self, form):
        messages.success(self.request, f"Welcome back, {form.get_user().get_username()}.")
        return super().form_valid(form)


class AccessLogoutView(LogoutView):
    def post(self, request, *args, **kwargs):
        messages.info(request, 'You have been logged out.')
        return super().post(request, *args, **kwargs)


def _log_access_event(device_access_id, access_status, mapping=None, access_type="normal", access_control_device=None):
    """Persist access decisions for auditing and troubleshooting."""
    AccessEventLog.objects.create(
        device_access_id=str(device_access_id) if device_access_id is not None else '',
        user=mapping if mapping else None,
        access_status=access_status,
        access_type=access_type,
        access_control_device=access_control_device,
        decision_type='manual' if mapping and mapping.manual_access_decision else 'auto',
    )


def _find_access_id(device_access_id, device):
    entries = AccessId.objects.select_related('mapping').filter(access_id=device_access_id)
    if device:
        return entries.filter(access_device=device).first() or entries.filter(
            Q(auth_type='card', is_manual=False) | Q(access_device__isnull=True)
        ).first()
    return entries.filter(Q(is_manual=False) | Q(access_device__isnull=True)).first()


def _require_staff(request):
    require_role(request, 'staff', 'admin')


@login_required
@require_http_methods(['GET'])
def portal_user_list_view(request):
    require_role(request, 'admin')
    users = get_user_model().objects.prefetch_related('groups').order_by('username', 'pk')
    query = request.GET.get('q', '').strip()
    if query:
        users = users.filter(
            Q(username__icontains=query) | Q(first_name__icontains=query)
            | Q(last_name__icontains=query) | Q(email__icontains=query)
        )
    page_obj = Paginator(users, ACCOUNT_MAPPING_PAGE_SIZE).get_page(request.GET.get('page'))
    rows = [{'account': user, 'role': get_assigned_role(user)} for user in page_obj]
    return render(request, 'access/portal_user_list.html', {
        'rows': rows, 'page_obj': page_obj, 'query': query,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def portal_user_form_view(request, pk=None):
    require_role(request, 'admin')
    User = get_user_model()
    with transaction.atomic():
        user = get_object_or_404(User.objects.select_for_update(), pk=pk) if pk else User()
        if user.is_superuser and not request.user.is_superuser:
            raise PermissionDenied
        form = PortalUserForm(
            request.POST if request.method == 'POST' else None,
            instance=user, actor=request.user,
        )
        if request.method == 'POST' and form.is_valid():
            user = form.save()
            if user.pk == request.user.pk and form.cleaned_data.get('password1'):
                update_session_auth_hash(request, user)
            logger.info('Portal user saved: user=%s role=%s active=%s operator=%s',
                        user.pk, form.cleaned_data['role'], user.is_active, request.user.pk)
            messages.success(request, 'Portal user updated.' if pk else 'Portal user created.')
            return redirect('portal_user_list')
    return render(request, 'access/portal_user_form.html', {
        'form': form, 'heading': 'Edit portal user' if pk else 'Create portal user',
    })


def _refresh_access_cache():
    global access_decisions_list
    access_decisions_list = get_access_decisions_list()


@login_required
@require_http_methods(['GET', 'POST'])
def mapping_create_view(request):
    _require_staff(request)
    form = ManualMappingForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            mapping = form.save(commit=False)
            mapping.account_user_id = f'local-{uuid4().hex}'
            mapping.accounting_system = 'local'
            mapping.is_manual = True
            mapping.save()
            _refresh_access_cache()
        messages.success(request, 'Local mapping created. You can now connect access IDs.')
        return redirect('account_mapping_detail', pk=mapping.pk)
    return render(request, 'access/mapping_form.html', {
        'form': form, 'heading': 'Create local mapping',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def mapping_edit_view(request, pk):
    _require_staff(request)
    mapping = get_object_or_404(AccountMapping, pk=pk)
    form_class = ManualMappingForm if mapping.is_manual else MappingClassificationForm
    form = form_class(request.POST if request.method == 'POST' else None, instance=mapping)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            mapping = form.save(commit=False)
            mapping.save(update_fields=[*form.Meta.fields, 'last_updated_at'])
            _refresh_access_cache()
        messages.success(request, 'Mapping updated.')
        return redirect('account_mapping_detail', pk=mapping.pk)
    return render(request, 'access/mapping_form.html', {
        'form': form, 'mapping': mapping,
        'heading': 'Edit local mapping' if mapping.is_manual else 'Edit mapping classification',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def mapping_access_id_view(request, pk, access_pk=None):
    _require_staff(request)
    mapping = get_object_or_404(AccountMapping, pk=pk)
    entry = get_object_or_404(AccessId, pk=access_pk, mapping=mapping, is_enabled=True) if access_pk else None
    form = ManualAccessIdForm(
        request.POST if request.method == 'POST' else None, mapping=mapping, instance=entry,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                AccountMapping.objects.select_for_update().get(pk=mapping.pk)
                list(AccessId.objects.select_for_update().filter(
                    access_id=form.cleaned_data['access_id'],
                ))
                if entry:
                    entry = get_object_or_404(
                        AccessId.objects.select_for_update(), pk=entry.pk, mapping=mapping, is_enabled=True,
                    )
                form = ManualAccessIdForm(request.POST, mapping=mapping, instance=entry)
                if not form.is_valid():
                    return render(request, 'access/mapping_form.html', {
                        'form': form, 'mapping': mapping,
                        'heading': 'Edit access ID' if entry else 'Connect access ID',
                    })
                values = form.cleaned_data
                identity = {'access_id': values['access_id'], 'access_device': values['access_device']}
                if entry and (entry.access_id, entry.access_device_id) != (
                    values['access_id'], values['access_device'].pk if values['access_device'] else None
                ):
                    if entry.access_id == values['access_id'] and values['access_device'] is None:
                        # The new shared manual row protects this ID on every device.
                        entry.delete()
                    else:
                        entry.is_enabled = False
                        entry.is_manual = True
                        entry.save(update_fields=['is_enabled', 'is_manual', 'last_updated_at'])
                if values['access_device'] is None:
                    # A shared reconnection supersedes disabled device-specific scopes.
                    AccessId.objects.filter(
                        access_id=values['access_id'], is_enabled=False, access_device__isnull=False,
                    ).delete()
                AccessId.objects.update_or_create(
                    **identity,
                    defaults={'mapping': mapping, 'auth_type': values['auth_type'],
                              'is_manual': True, 'is_enabled': True},
                )
                _refresh_access_cache()
        except IntegrityError:
            logger.exception('Conflicting access ID while editing mapping %s', mapping.pk)
            form.add_error(None, 'This access ID was changed by another operation. Reload and try again.')
        else:
            messages.success(request, 'Access ID connected and protected from external sync.')
            return redirect('account_mapping_detail', pk=mapping.pk)
    return render(request, 'access/mapping_form.html', {
        'form': form, 'mapping': mapping,
        'heading': 'Edit access ID' if entry else 'Connect access ID',
    })


@login_required
@require_POST
def mapping_access_id_remove_view(request, pk, access_pk):
    _require_staff(request)
    with transaction.atomic():
        entry = get_object_or_404(
            AccessId.objects.select_for_update(), pk=access_pk, mapping_id=pk, is_enabled=True,
        )
        entry.is_enabled = False
        entry.is_manual = True
        entry.save(update_fields=['is_enabled', 'is_manual', 'last_updated_at'])
        _refresh_access_cache()
    messages.success(request, 'Access ID disconnected. External sync will not reconnect it.')
    return redirect('account_mapping_detail', pk=pk)


def get_test_users():
    """Fetch users from the mock external accounting DB and merge mapping state."""
    external_db_path = Path(settings.BASE_DIR) / EXTERNAL_ACCOUNTING_DB_NAME
    if not external_db_path.exists():
        return []

    try:
        with closing(sqlite3.connect(external_db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT account_user_id, full_name, balance
                FROM accounting_users
                ORDER BY full_name
                '''
            )
            external_users = cursor.fetchall()
    except sqlite3.Error:
        return []

    mapping_lookup = {
        mapping.account_user_id: mapping.device_access_id
        for mapping in AccountMapping.objects.prefetch_related('access_ids').all()
    }

    users = []
    for account_user_id, full_name, balance in external_users:
        mapped_device_access_id = mapping_lookup.get(str(account_user_id))
        users.append(
            {
                'account_user_id': str(account_user_id),
                'full_name': full_name,
                'balance': float(balance),
                'device_access_id': mapped_device_access_id,
                'connected': bool(mapped_device_access_id),
            }
        )

    print(users)  # Debug log to verify users data

    return users


@login_required
def account_mapping_list_view(request):
    """Return a list of all mappings of the access control user to the different external system accounts for display in the admin interface"""

    # User data is read from the mock external accounting SQLite DB and merged with current mapping state.
    # TODO : Add filtering and pagination as needed for real implementation when pulling from actual database with potentially large number of users.

    user_list = AccountMapping.objects.all().order_by('-last_name', '-first_name')
    mapping_filter = AccountMappingFilter(request.GET, queryset=user_list)

    paginator = Paginator(mapping_filter.qs.order_by('narration', '-last_name', '-first_name'), ACCOUNT_MAPPING_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    last_updated = AccountMapping.objects.order_by('-last_updated_at').first()
    system_metadata = SystemMetadata.objects.first()    
    mapping_last_updated_at = system_metadata.last_successful_mapping_update_at if system_metadata else None
    access_device_last_synced_at = system_metadata.last_device_sync_at if system_metadata else None

    last_access_event = AccessEventLog.objects.order_by('-event_timestamp').first()
    last_access_event_timestamp = last_access_event.event_timestamp if last_access_event else None
    last_access_decision = last_access_event.access_status if last_access_event else '-'
    return render(
        request,
        "access/mapping_list.html",
        {
            "users": page_obj,
            "total_users": mapping_filter.qs.count(),
            "connected_users": mapping_filter.qs.filter(access_ids__is_enabled=True, access_ids__access_id__gt='').distinct().count(),
            "connected_access_ids": AccessId.objects.filter(mapping__in=mapping_filter.qs, is_enabled=True, access_id__gt='').count(),
            "not_connected_users": mapping_filter.qs.exclude(pk__in=AccessId.objects.filter(is_enabled=True, access_id__gt='', mapping__isnull=False).values('mapping_id')).count(),
            "page_obj": page_obj,
            "mapping_filter": mapping_filter,
            "query_string": query_string,
            "mapping_last_updated": mapping_last_updated_at if mapping_last_updated_at else "-",
            "access_device_last_synced_at": access_device_last_synced_at if access_device_last_synced_at else "-",
            "last_access_event_timestamp": last_access_event_timestamp if last_access_event_timestamp else "-",
            "last_access_decision": last_access_decision,
        },
    )


@login_required
def account_mapping_detail_view(request, pk):
    """Display a mapping's current connections and recorded access history."""
    mapping = get_object_or_404(AccountMapping, pk=pk)
    access_ids = mapping.access_ids.filter(is_enabled=True).select_related('access_device').order_by('created_at', 'pk')
    events = AccessEventLog.objects.filter(user=mapping).select_related('access_control_device').order_by('-event_timestamp', '-pk')
    event_stats = events.aggregate(
        total=Count('pk'),
        granted=Count('pk', filter=Q(access_status='grant')),
        rejected=Count('pk', filter=Q(access_status='reject')),
    )
    page_obj = Paginator(events, MAPPING_EVENT_PAGE_SIZE).get_page(request.GET.get('page'))

    return render(
        request,
        'access/mapping_detail.html',
        {
            'mapping': mapping,
            'access_ids': access_ids,
            'disconnected_ids': mapping.access_ids.filter(is_enabled=False).select_related('access_device'),
            'connected': mapping.has_access_id,
            'event_stats': event_stats,
            'latest_event': events.first(),
            'page_obj': page_obj,
        },
    )


@login_required
@require_POST
def account_mapping_override_view(request, pk):
    _require_staff(request)
    override = request.POST.get('access_override')
    if override not in dict(AccountMapping.ACCESS_OVERRIDE_CHOICES):
        return HttpResponseBadRequest('Invalid access override. Choose Auto, Grant, or Reject.')

    global access_decisions_list
    with transaction.atomic():
        mapping = get_object_or_404(AccountMapping.objects.select_for_update(), pk=pk)
        mapping.access_override = override
        mapping.save(update_fields=['access_override', 'last_updated_at'])
        decisions = get_access_decisions_list()
    access_decisions_list = decisions
    logger.info(
        "Mapping access override changed: mapping=%s override=%s operator=%s",
        mapping.pk, override, request.user.pk,
    )
    messages.success(request, f'Access decision for {mapping.get_full_name()} set to {mapping.get_access_override_display()}.')
    if request.POST.get('return_to') == 'list':
        url = redirect('account_mapping_list').url
        query = request.POST.get('list_query', '')
        return redirect(f'{url}?{query}' if query else url)
    return redirect('account_mapping_detail', pk=mapping.pk)


@login_required
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    """View to display and update settings"""
    require_role(request, 'admin')
    settings_obj = Setting.get_solo()
    
    if request.method == 'POST':
        form = SettingForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings updated successfully!')

            # refresh the access decisions cache after settings update to ensure that any changes to the authorization flow or balance thresholds are reflected in the access decisions being provided to the access control devices at the time of the access events based on the latest settings
            
            global access_decisions_list
            access_decisions_list = get_access_decisions_list()
            print("Access decisions cache refreshed after settings update.")  # Debug log to verify access decisions cache refresh after settings update

            return redirect('account_mapping_list')
    else:
        form = SettingForm(instance=settings_obj)
    
    return render(request, 'access/settings.html', {'form': form})


def get_accounting_balances(device_access_id, currency=None):
    """Return usd and/or zwg balances from the external accounting data for the mapped device_access_id."""
    # get user id from mapping and then get balance for that user from the external accounting system
    access_id_entry = AccessId.objects.select_related('mapping').filter(access_id=device_access_id).first()
    mapping = access_id_entry.mapping if access_id_entry else None
    if not mapping:
        return None
    try:
        # r = requests.get(f'{EXTERNAL_ACCOUNTING_URL}/api/UserData/get-data-by-cardId/{mapping.account_user_id}', timeout=10)
        # r.raise_for_status()
        # user_data = r.json()

        # get from database for testing purposes since we are not currently updating balances in the AccountMapping table with real-time data from the external system in this demo implementation, but in a real implementation we would want to ensure we are fetching the latest balance data from the external system at the time of the access event to make accurate access decisions based on the most up-to-date user data

        user_data = mapping
        print(f"Fetched user data for device_access_id {device_access_id} from external accounting system:", user_data)  # Debug log to verify response from external system

        # log last successful balance fetch time for visibility in admin interface and troubleshooting purposes
        SystemMetadata.objects.update_or_create(single_instance_id="singleton", defaults={"last_successful_balance_fetch_at": timezone.now()})

        if currency == 'USD':
            return user_data.get('usdBalance') if type(user_data) is dict else user_data.usd_balance
        elif currency == 'ZWG':
            return user_data.get('zwdBalance') if type(user_data) is dict else user_data.zwg_balance
        else:
            return {
                'usd_balance': user_data.get('usdBalance') if type(user_data) is dict else user_data.usd_balance,
                'zwg_balance': user_data.get('zwdBalance') if type(user_data) is dict else user_data.zwg_balance,
            }
        
    except HTTPError as e:
        print(f"HTTP error occurred while fetching user data for device_access_id {device_access_id} from external accounting system:", e)  # Debug log for HTTP errors
        return None
    except requests.RequestException as e:
        print(f"Error fetching user data for device_access_id {device_access_id} from external accounting system:", e)
        return None
    

def get_access_decisions_list():
    """Return list of all access id entries with their access decisions for local device caching purposes at the access control device level to allow it to make access decisions even if there are transient issues with the connection to the server or external accounting system"""
    
    access_decission_list = []

    settings = Setting.get_solo()
    staff_access_decission_list = []
    for staff_id in STAFF_USER_IDS:
        entry = AccessId.objects.select_related('mapping').filter(
            Q(auth_type='card', is_manual=False) | Q(access_device__isnull=True),
            access_id=str(staff_id),
        ).first()
        staff_access_decission_list.append({
            "device_access_id": str(staff_id),
            "access": _access_id_decision(entry, settings) if entry else get_access_decision(str(staff_id), None, settings),
        })
    access_decission_list.append({"staff": staff_access_decission_list})  # Add staff access decisions to the main access decisions list under a separate key for clarity and to avoid mixing with regular user access decisions, and to allow for easy filtering of staff access decisions in the access control device if needed

    print("Staff access id entries added to access id entry list for GET request to access_event_view:", staff_access_decission_list)  # Debug log to verify staff access id entries being added to the response

    print("Updated access decisions list with staff access decisions for GET request to access_event_view:", access_decission_list)  # Debug log to verify access decisions list after adding staff access decisions

    # append for all access ids of auth type card
    card_access_decission_list = []
    for access_id_entry in AccessId.objects.select_related('mapping').filter(
        Q(auth_type="card", is_manual=False) | Q(access_device__isnull=True)
    ).exclude(access_id__isnull=True).exclude(access_id=''):
        mapping = access_id_entry.mapping
        access_decision = _access_id_decision(access_id_entry, settings)
        card_access_decission_list.append({
            "device_access_id": access_id_entry.access_id,
            "access": access_decision
        })
    print("Card access id entries added to access id entry list for GET request to access_event_view:", card_access_decission_list)  # Debug log to verify card access id entries being added to the response
    access_decission_list.append({"card": card_access_decission_list})  # Add card access decisions to the main access decisions list under a separate key for clarity and to avoid mixing with biometric access decisions, and to allow for easy filtering of card access decisions in the access control device if needed

    # Device-specific entries take precedence when an ID is enrolled on multiple devices.
    for access_control_device in AccessControlDevice.objects.filter(is_active=True):
        device_biometric_access_decission_list = []
        for access_id_entry in access_control_device.access_ids.select_related('mapping').exclude(
            access_id__isnull=True
        ).exclude(access_id=''):
            mapping = access_id_entry.mapping
            access_decision = _access_id_decision(access_id_entry, settings)
            device_biometric_access_decission_list.append({
                "device_access_id": access_id_entry.access_id,
                "access": access_decision
            })
        print("Biometric access id entries added to access id entry list for GET request to access_event_view:", device_biometric_access_decission_list)  # Debug log to verify biometric access id entries being added to the response
        access_decission_list.append({access_control_device.device_id: device_biometric_access_decission_list})  # Add biometric access decisions to the main access decisions list under a separate key for clarity and to avoid mixing with card access decisions, and to allow for easy filtering of biometric access decisions in the access control device if needed

    # save the compiled access decisions list to the database for visibility in the admin interface and troubleshooting purposes, and to allow for analysis of access decisions being provided to the access control devices over time based on changes to user data and settings
    SystemMetadata.objects.update_or_create(single_instance_id="singleton", defaults={"access_decision_cache": access_decission_list})

    print("Compiled access decisions list for GET request to access_event_view:", access_decission_list)  # Debug log to verify final compiled access decisions list being returned in the response

    return access_decission_list


def _access_id_decision(entry, settings):
    if not entry.is_enabled:
        return "REJECT"
    return get_access_decision(entry.access_id, entry.mapping, settings)


def get_access_decision(device_access_id, mapping, settings):
    """Return access decision based on the authorization flow setting and relevant balance thresholds if applicable"""
    
    if mapping and mapping.manual_access_decision:
        return mapping.manual_access_decision
    if mapping and mapping.classification == 'staff':
        return "GRANT"
    if str(device_access_id) in STAFF_USER_IDS:
        return "GRANT"
    if not mapping:
        return "REJECT"

    authorization_flow = settings.authorization_flow
    print(f"Authorization flow setting: {authorization_flow} for device_access_id: {device_access_id} ({mapping.narration})")  # Debug log to verify which authorization flow is being applied for the access decision

    match authorization_flow:
        case "grant_all":
            # print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
            print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
            return "GRANT"
        case "reject_all":
            print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
            print("Rejected via", authorization_flow) # Debug log to verify which authorization flow is rejecting access
            return "REJECT"
        case "check_usd_balance" | "check_zwg_balance":
            
            filter_currency = 'usd' if authorization_flow == "check_usd_balance" else 'zwg'
            applicable_balance = getattr(mapping, f'{filter_currency}_balance')
            applicable_balance_threshold = getattr(settings, f'{filter_currency}_balance_threshold')
            print(f"Applicable balance threshold for {filter_currency.upper()}: {applicable_balance_threshold}")  # Debug log to verify which balance threshold is applicable for the access decision

            if applicable_balance is not None and applicable_balance >= applicable_balance_threshold:
                print("Values ", applicable_balance, applicable_balance_threshold)  # Debug log to verify balance values for access decision
                # grant access if user meets balance threshold
                print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                return "GRANT"
            else:
                # fetch latest balance from external system in case it has changed since last fetch and check again before rejecting access
                current_balance = get_accounting_balances(device_access_id, currency=filter_currency.upper())

                print(f"Fetched current balance for device_access_id {device_access_id} from external accounting system: {current_balance}")  # Debug log to verify response from external system
                print(f"Access ID: {device_access_id}, Palladium ID: {mapping.account_user_id}, {filter_currency.upper()} balance: {current_balance}")  # Debug log

                if current_balance is None:
                    # if there was an error fetching balance data from the external system, default to granting access to avoid locking out users due to transient issues with the external system, but log the event for troubleshooting
                    print("Error fetching balance data for device_access_id:", device_access_id, "Defaulting to GRANT access but check logs for troubleshooting.")  # Debug log
                    print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                    return "GRANT"
                elif current_balance >= applicable_balance_threshold:
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                    return "GRANT"
                else:
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    print("Rejected via", authorization_flow) # Debug log to verify which authorization flow is rejecting access
                    return "REJECT"
        case "check_usd_or_zwg_balance":
            # check if user meets either balance threshold to grant access
            if ((mapping.usd_balance is not None and mapping.usd_balance >= settings.usd_balance_threshold) or 
                (mapping.zwg_balance is not None and mapping.zwg_balance >= settings.zwg_balance_threshold)):
                print(f"Access ID: {device_access_id}, Palladium ID: {mapping.account_user_id}, USD balance: {mapping.usd_balance}/{settings.usd_balance_threshold}, ZWG balance: {mapping.zwg_balance}/{settings.zwg_balance_threshold}")  # Debug log
                print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                return "GRANT"
            else:
                # fetch latest balances from external system in case they have changed since last fetch and check again before rejecting access
                current_balances = get_accounting_balances(device_access_id)

                print(f"Access ID: {device_access_id}, Palladium ID: {mapping.account_user_id}, Current balances: {current_balances}")  # Debug log

                if current_balances is None:
                    # if there was an error fetching balance data from the external system, default to granting access to avoid locking out users due to transient issues with the external system, but log the event for troubleshooting
                    print("Error fetching balance data for device_access_id:", device_access_id, "Defaulting to GRANT access but check logs for troubleshooting.")  # Debug log
                    print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                    return "GRANT"
                elif ((current_balances.get('usd_balance') is not None and current_balances.get('usd_balance') >= settings.usd_balance_threshold) or 
                        (current_balances.get('zwg_balance') is not None and current_balances.get('zwg_balance') >= settings.zwg_balance_threshold)):
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    print("Granted via", authorization_flow) # Debug log to verify which authorization flow is granting access
                    return "GRANT"
                else:
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    print("Rejected via", authorization_flow) # Debug log to verify which authorization flow is rejecting access
                    return "REJECT"
        case _:
            print("Invalid authorization flow setting. Defaulting to REJECT access for device_access_id:", device_access_id)  # Debug log
            return "REJECT"


@csrf_exempt
def access_event_view(request, device_serial_number=None):
    """Handle access event from access control device"""
    global access_decisions_list
    print(f"Received request at access_event_view with method {request.method} and device_serial_number: {device_serial_number}")  # Debug log to verify incoming requests and device serial number

    access_control_device = AccessControlDevice.objects.filter(device_id=device_serial_number).first() if device_serial_number else None

    if access_control_device and access_control_device.is_active == False:
        print(f"Access control device {device_serial_number} is inactive. Rejecting access event.")  # Debug log to verify that events from inactive devices are being rejected
        return JsonResponse({"access": "REJECT"}, status=HTTPStatus.OK)

    if request.method == "POST":

        try:
            print("Received access event:", request.body)  # Debug log

            # Parse the incoming JSON data
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log
            
            # get device_access_id from JSON data
            device_access_id = data.get("access_id") # could be a user_id or card_id depending on the access control system
            print("Extracted device_access_id:", device_access_id)  # Debug log

            device_access_id = str(device_access_id)  # Ensure device_access_id is a string for consistent processing and logging

            # log tamper events
            tamper_events = ["TAMPER_OPEN", "TAMPER_RESTORED", "tamper_enabled", "tamper_disabled"]
            is_tamper_event = False
            for tamper_event_prefix in tamper_events:
                is_tamper_event = True if device_access_id.startswith(tamper_event_prefix) else False
                if is_tamper_event:
                    TamperEventLog.objects.create(event_type=tamper_event_prefix, access_control_device=access_control_device if access_control_device else None)
                    print("Logged tamper event:", tamper_event_prefix)  # Debug log
                    return JsonResponse({"status": "tamper event logged"}, status=HTTPStatus.OK)
            
            # handle mapping of wiegand and card uid for access control devices whose user ids we do have access to beforehand but the users where already enrolled on the device.
            if device_access_id.startswith("CARD-"):
                # enter new mapping mode
                # extract card uid and userid
                mapping_ids = device_access_id.lstrip("CARD-").split("-WIEGAND-")
                card_uid = mapping_ids[0] if len(mapping_ids) > 0 else None
                wiegand_id = mapping_ids[1] if len(mapping_ids) > 1 else None
                print(f"Extracted card_uid: {card_uid}, wiegand_id: {wiegand_id} from device_access_id: {device_access_id}")  # Debug log

                # get mapping using card uid
                access_id_entry = _find_access_id(card_uid, access_control_device)
                mapping = access_id_entry.mapping if access_id_entry else None

                # map wiegand id to the same user if there is a mapping for the card uid and grand access, otherwise reject access
                if access_id_entry and access_id_entry.is_enabled and mapping and wiegand_id:
                    existing = _find_access_id(wiegand_id, access_control_device)
                    if existing and (
                        not existing.is_enabled or existing.mapping_id != mapping.pk
                    ):
                        _log_access_event(wiegand_id, "reject", mapping, access_type="one_step_mapping", access_control_device=access_control_device)
                        return JsonResponse({"access": "REJECT"}, status=HTTPStatus.OK)
                    if not existing:
                        AccessId.objects.create(
                            access_id=wiegand_id, auth_type="biometric", mapping=mapping,
                            access_device=access_control_device,
                        )
                    decision = mapping.manual_access_decision or "GRANT"
                    _log_access_event(wiegand_id, decision.lower(), mapping, access_type="one_step_mapping", access_control_device=access_control_device if access_control_device else None)
                    return JsonResponse({"access": decision}, status=HTTPStatus.OK)
                else:
                    if wiegand_id:
                        _log_access_event(wiegand_id, "reject", mapping, access_type="one_step_mapping", access_control_device=access_control_device if access_control_device else None)
                        print("Access REJECTED for wiegand_id:", wiegand_id)  # Debug log
                    else:
                        print("Wiegand ID not provided in device_access_id, cannot process mapping. device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "REJECT"}, status=HTTPStatus.OK)

            # normal access event flow for devices without wiegand/card uid mapping scenario
            access_id_entry = _find_access_id(device_access_id, access_control_device)
            mapping = access_id_entry.mapping if access_id_entry else None

            settings = Setting.get_solo()
            access_decision = _access_id_decision(access_id_entry, settings) if access_id_entry else get_access_decision(device_access_id, None, settings)

            _log_access_event(device_access_id, access_decision.lower(), mapping, access_control_device=access_control_device if access_control_device else None)
            return JsonResponse({"access": access_decision}, status=HTTPStatus.OK)
                                
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        
    elif request.method == "GET":
        # return list of all mappings against access decision for local device caching purposes at the access control device level to allow it to make access decisions even if there are transient issues with the connection to the server or external accounting system.

        # A worker-local cache cannot observe overrides changed by another worker.
        access_decisions_list = get_access_decisions_list()

        computed_access_decisions_list = []
        # add all staff and card access decisions to the computed access decisions list for local device caching purposes at the access control device level
        for category in access_decisions_list:
            if category.get("staff"):
                computed_access_decisions_list.extend(category.get("staff", []))
            elif category.get("card"):
                computed_access_decisions_list.extend(category.get("card", []))
            elif device_serial_number and category.get(device_serial_number):
                computed_access_decisions_list.extend(category.get(device_serial_number, []))

        computed_access_decisions_list = list({
            decision["device_access_id"]: decision
            for decision in computed_access_decisions_list
        }.values())

        # log sync event for visibility in admin interface and troubleshooting purposes
        AccessDeviceSyncLog.objects.create(
            device=access_control_device if access_control_device else None,
            success=True if access_decisions_list else False,
            timestamp=timezone.now(),
        )

        # log last sync time for access device in system metadata for visibility in admin interface and troubleshooting purposes
        SystemMetadata.objects.update_or_create(single_instance_id="singleton", defaults={"last_device_sync_at": timezone.now()})

        # temporarily fail the request to prevent device from updating
        # return JsonResponse({"status": "temporary error"}, status=HTTPStatus.BAD_REQUEST)
        return JsonResponse({"mappings": computed_access_decisions_list}, status=HTTPStatus.OK)

    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)


@login_required
@csrf_protect
def api_account_mapping_view(request, filter_name=None):
    """API endpoint to return list of mappings based on filter_name (e.g. pending, update) or to create/update/delete mapping based on request method and filter_name (e.g. account_user_id for delete)"""
    _require_staff(request)
    if request.method == 'POST' and filter_name == 'update':
        require_role(request, 'admin')
    match request.method:
        case "GET":
            return handle_get_mappings(request, filter_name)
        case "POST":
            return handle_create_mapping(request, filter_name)
        case "DELETE":
            return handle_delete_mapping(request, filter_name)
        case _:
            return JsonResponse({"error": "Invalid request method"}, status=HTTPStatus.METHOD_NOT_ALLOWED)


def handle_get_mappings(request, filter_name=None):
    """Handle GET request to return pending mapping or all mappings based on filter_name"""
    if filter_name is None:
        # return message that filter_name is required for GET requests
        return JsonResponse({"error": "filter_name is required for GET request"}, status=HTTPStatus.BAD_REQUEST)
    else:
        if filter_name == "pending":
            temp = PendingAccountMapping.objects.first()
            return JsonResponse({"device_access_id": temp.device_access_id if temp else None}, status=HTTPStatus.OK)
        else:
            # return message that only pending filter is supported for GET requests
            return JsonResponse({"error": "Invalid filter for GET request. Only 'pending' is supported."}, status=HTTPStatus.BAD_REQUEST)


@csrf_protect
def handle_create_mapping(request, filter_name=None):
    """Handle POST request to create/update mapping between access control user and accounting system user"""
    if filter_name == "update":
        return fetch_external_users()  # Trigger fetch from external system to update user data before updating mapping
    _require_staff(request)
    
    try:
        data = json.loads(request.body)
        print("JSON data:", data)  # Debug log

        device_access_id = data.get("device_access_id")
        account_user_id = data.get("account_user_id")
        print("Extracted data:", device_access_id, account_user_id)  # Debug log

        mapping = AccountMapping.objects.filter(account_user_id=account_user_id).first()
        if not mapping:
            return JsonResponse({"error": "No accounting user found for the provided account_user_id"}, status=HTTPStatus.BAD_REQUEST)

        existing_access = AccessId.objects.filter(access_id=device_access_id).first()
        if existing_access and existing_access.mapping_id != mapping.id:
            return JsonResponse({"error": "This device's Access ID is already mapped to another account"}, status=HTTPStatus.BAD_REQUEST)

        if not device_access_id or not str(device_access_id).strip():
            return JsonResponse({"error": "Access ID is required"}, status=HTTPStatus.BAD_REQUEST)
        AccessId.objects.update_or_create(
            access_id=device_access_id, access_device=None,
            defaults={"mapping": mapping, "is_manual": True, "is_enabled": True},
        )
        _refresh_access_cache()
        PendingAccountMapping.objects.all().delete()
        return JsonResponse({"status": "created"}, status=HTTPStatus.CREATED)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST)


@csrf_protect
def handle_delete_mapping(request, filter_name=None):
    """Handle DELETE request to remove mapping between access control user and accounting system user (set device_access_id to null for the given account_user_id)"""
    _require_staff(request)
    if filter_name is None:
        return JsonResponse({"error": "account_user_id is required for DELETE request"}, status=HTTPStatus.BAD_REQUEST) 

    mapping = AccountMapping.objects.filter(account_user_id=filter_name).first()
    if not mapping:
        return JsonResponse({"error": "No mapping found for that account user."}, status=HTTPStatus.NOT_FOUND)

    mapping.access_ids.update(is_enabled=False, is_manual=True, last_updated_at=timezone.now())
    _refresh_access_cache()
    return JsonResponse({"status": "unmapped"}, status=HTTPStatus.NO_CONTENT)


def sync_external_users():
    """Fetch external users and upsert local mapping records."""

    try:
        r = requests.get(f'{EXTERNAL_ACCOUNTING_URL}', timeout=60)
        r.raise_for_status()
        users = r.json()
        print(f"Fetched {len(users)} users from external accounting system:", users)  # Debug log to verify response from external system 

        # for user in users create or update AccessId entry with the corresponding mapping to ensure we have the most up-to-date user data and mapping state for access decisions at the time of the access event, but in a real implementation we would want to optimize this to only update the entries that have changed since the last fetch to minimize database writes and potential performance impact
        for user in users:

            # log to file the whole user data object values for each user from external system to verify data structure and values before creating/updating mapping entries
            with open("mapping_log.txt", "a") as f:
                f.write(f"Processing user from external system with account number: {user.get('accountNumber')}, first name: {user.get('firstName')}, last name: {user.get('lastName')}, member name: {user.get('memberName')}, usd balance: {user.get('usdBalance')}, zwg balance: {user.get('zwdBalance')} at {timezone.now()}\n")    

            # create of update AccountMapping entry for the user
            mapping, created = AccountMapping.objects.get_or_create(
                account_user_id=str(user.get('accountNumber')), # Assuming accountNumber is the unique identifier for the user in the external system
                defaults={
                    'first_name': user.get('firstName', ''),
                    'last_name': user.get('lastName', ''),
                    'narration': str(user.get('memberName', '')).upper(),
                    'usd_balance': user.get('usdBalance', -9999999.00),
                    'zwg_balance': user.get('zwdBalance', -9999999.00),
                    'last_updated_at': timezone.now(),
                }
            )
            if mapping.is_manual:
                logger.info("External sync skipped local mapping %s", mapping.pk)
                continue
            if not created:
                AccountMapping.objects.filter(pk=mapping.pk, is_manual=False).update(
                    first_name=user.get('firstName', ''),
                    last_name=user.get('lastName', ''),
                    narration=str(user.get('memberName', '')).upper(),
                    usd_balance=user.get('usdBalance', -9999999.00),
                    zwg_balance=user.get('zwdBalance', -9999999.00),
                    last_updated_at=timezone.now(),
                )
                mapping.refresh_from_db()
            # print(f"Account number: {user.get('accountNumber')}. {'Created' if created else 'Updated'} AccountMapping for account_user_id {mapping.account_user_id} with first_name: {mapping.first_name}, last_name: {mapping.last_name}, narration: {mapping.narration}, usd_balance: {mapping.usd_balance}, zwg_balance: {mapping.zwg_balance}")  # Debug log to verify creation or update of AccountMapping entry for each user from external system

            # log creation or update of AccountMapping entry for each user from external system to file
            with open("mapping_log.txt", "a") as f:
                f.write(f"Account number: {user.get('accountNumber')}. {'Created' if created else 'Updated'} AccountMapping for account_user_id {mapping.account_user_id} with first_name: {mapping.first_name}, last_name: {mapping.last_name}, narration: {mapping.narration}, usd_balance: {mapping.usd_balance}, zwg_balance: {mapping.zwg_balance} at {timezone.now()}\n")

            # create or update AccessId entry
            raw_card_id = user.get('cardId')
            card_id = str(raw_card_id).strip() if raw_card_id is not None else ''
            if not card_id:
                logger.warning("External sync skipped blank card ID for mapping %s", mapping.pk)
                continue
            with transaction.atomic():
                candidates = list(AccessId.objects.select_for_update().filter(access_id=card_id))
                if any(entry.is_manual or (entry.mapping_id and entry.mapping.is_manual) for entry in candidates):
                    logger.info("External sync skipped protected access ID for mapping %s", mapping.pk)
                    continue
                access, access_created = AccessId.objects.get_or_create(
                    access_id=card_id, access_device=None,
                    defaults={"auth_type": "card", "mapping": mapping},
                )
                if not access_created:
                    access = AccessId.objects.select_for_update().get(pk=access.pk)
                    if access.is_manual or (access.mapping_id and access.mapping.is_manual):
                        logger.info("External sync skipped concurrently protected access ID for mapping %s", mapping.pk)
                        continue
                    access.auth_type = 'card'
                    access.mapping = mapping
                    access.save(update_fields=['auth_type', 'mapping', 'last_updated_at'])

            # log creation or update of AccessId entry for each user from external system to file
            with open("mapping_log.txt", "a") as f:
                f.write(f"Account number: {user.get('accountNumber')}. {'Created' if access_created else 'Updated'} AccessId for access_id {access.access_id} with auth_type: {access.auth_type} mapped to account_user_id {mapping.account_user_id} at {timezone.now()}\n")

        # deletions for biometric entries will be handled manually through a separate admin interface for now since we don't have a unique identifier for those users from the external system in this demo implementation, but in a real implementation we would want to ensure we are also handling deletions and updates for all types of entries based on the data from the external system to keep the mapping state up-to-date and accurate for access decisions

        # update cached access decisions in system metadata and update in memory list to optimize access decision response time for access control device and log fetch event in system metadata for visibility in admin interface and troubleshooting purposes

        global access_decisions_list
        access_decisions_list = get_access_decisions_list()  # refresh in-memory cache with latest data after sync

        SystemMetadata.objects.update_or_create(
            single_instance_id="singleton", 
            defaults={
                "last_successful_mapping_update_at": timezone.now(),
                "access_decision_cache": access_decisions_list
            }
        )

        print("External users synced and local mapping records updated successfully.")  # Debug log to confirm successful sync
        print(f"Access decisions cache updated with {len(access_decisions_list)} entries after syncing external users.")  # Debug log to verify access decisions cache update after syncing external users

        return {
            "ok": True,
            "status": "external users updated",
            "updated_count": len(users),
        }
    except HTTPError as e:
        print("HTTP error occurred while fetching users from external accounting system:", e)  # Debug log for HTTP errors
        logger.exception("HTTP error occurred while fetching users from external accounting system")
        return {
            "ok": False,
            "error": "Failed to fetch users from external accounting system due to HTTP error",
            "status": HTTPStatus.BAD_GATEWAY,
        }
    except RequestException as e:
        print("Error fetching users from external accounting system:", e)  # Debug log for error handling
        logger.exception("Error fetching users from external accounting system")
        return {
            "ok": False,
            "error": "Failed to fetch users from external accounting system",
            "status": HTTPStatus.INTERNAL_SERVER_ERROR,
        }


def fetch_external_users():
    """HTTP endpoint helper that syncs users from the external accounting system."""

    result = sync_external_users()
    if result.get("ok"):
        return JsonResponse({"status": result["status"], "updated_count": result["updated_count"]}, status=HTTPStatus.OK)

    return JsonResponse({"error": result.get("error", "Failed to fetch users from external accounting system")}, status=result.get("status", HTTPStatus.INTERNAL_SERVER_ERROR))

    
@login_required
@csrf_protect
def set_modal_state_view(request):
    """API endpoint to set the state of the mapping modal (open/closed)"""
    _require_staff(request)
    if request.method == "POST":
        print("Received modal state update:", request.body)  # Debug log
        try:
            data = json.loads(request.body)
            state = data.get("state")

            MappingModalState.objects.update_or_create(id=1, defaults={"state": state})

            if state == "closed":
                PendingAccountMapping.objects.all().delete()

            return JsonResponse({"status": "created"} , status=HTTPStatus.CREATED)
        
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=HTTPStatus.METHOD_NOT_ALLOWED)


@csrf_exempt
def dump_database_view(request):
    """API endpoint to dump the database for debugging purposes, protected by a simple secret key check since this is a potentially sensitive operation that should not be exposed publicly"""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            secret_key = data.get("secret_key")

            if secret_key != DUMP_DATABASE_SECRET_KEY:
                return JsonResponse({"error": "Invalid secret key"}, status=HTTPStatus.UNAUTHORIZED)

            # Dump the database using Django's dumpdata command
            from django.core import management
            from io import StringIO

            out = StringIO()
            management.call_command('dumpdata', stdout=out)
            database_dump = out.getvalue()

            return JsonResponse({"database_dump": database_dump}, status=HTTPStatus.OK)
        
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=HTTPStatus.METHOD_NOT_ALLOWED)
