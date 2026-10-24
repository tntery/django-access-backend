import json
import sqlite3
import environ
import requests
import logging
from requests.exceptions import RequestException, HTTPError
from http import HTTPStatus
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from .filters import AccountMappingFilter
from .models import AccessControlDevice, AccessDeviceSyncLog, AccessEventLog, TamperEventLog, AccessId, AccountMapping, PendingAccountMapping, MappingModalState, Setting, SystemMetadata
from .forms import SettingForm

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
    )


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
    mapping_last_updated_at = system_metadata.last_successful_mapping_update_at
    access_device_last_synced_at = system_metadata.last_device_sync_at

    last_access_event = AccessEventLog.objects.order_by('-event_timestamp').first()
    last_access_event_timestamp = last_access_event.event_timestamp if last_access_event else None
    last_access_decision = last_access_event.access_status if last_access_event else '-'
    return render(
        request,
        "access/mapping_list.html",
        {
            "users": page_obj,
            "total_users": mapping_filter.qs.count(),
            "connected_users": mapping_filter.qs.filter(access_ids__access_id__isnull=False).exclude(access_ids__access_id='').count(),
            "connected_access_ids": AccessId.objects.filter(mapping__in=mapping_filter.qs, access_id__isnull=False).exclude(access_id='').count(),
            "not_connected_users": mapping_filter.qs.filter(access_ids__access_id__isnull=True).count(),
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
    access_ids = mapping.access_ids.select_related('access_device').order_by('created_at', 'pk')
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
            'connected': mapping.has_access_id,
            'event_stats': event_stats,
            'latest_event': events.first(),
            'page_obj': page_obj,
        },
    )


@login_required
def settings_view(request):
    """View to display and update settings"""
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

    staff_access_decission_list = [{"device_access_id": str(staff_id), "access": "GRANT"} for staff_id in STAFF_USER_IDS]
    access_decission_list.append({"staff": staff_access_decission_list})  # Add staff access decisions to the main access decisions list under a separate key for clarity and to avoid mixing with regular user access decisions, and to allow for easy filtering of staff access decisions in the access control device if needed

    print("Staff access id entries added to access id entry list for GET request to access_event_view:", staff_access_decission_list)  # Debug log to verify staff access id entries being added to the response

    print("Updated access decisions list with staff access decisions for GET request to access_event_view:", access_decission_list)  # Debug log to verify access decisions list after adding staff access decisions

    settings = Setting.get_solo()

    # append for all access ids of auth type card
    card_access_decission_list = []
    for access_id_entry in AccessId.objects.filter(auth_type="card"):
        mapping = access_id_entry.mapping
        access_decision = get_access_decision(access_id_entry.access_id, mapping, settings)
        card_access_decission_list.append({
            "device_access_id": access_id_entry.access_id,
            "access": access_decision
        })
    print("Card access id entries added to access id entry list for GET request to access_event_view:", card_access_decission_list)  # Debug log to verify card access id entries being added to the response
    access_decission_list.append({"card": card_access_decission_list})  # Add card access decisions to the main access decisions list under a separate key for clarity and to avoid mixing with biometric access decisions, and to allow for easy filtering of card access decisions in the access control device if needed

    # append by device id for all access ids of auth type biometric
    for access_control_device in AccessControlDevice.objects.filter(is_active=True):
        device_biometric_access_decission_list = []
        for access_id_entry in access_control_device.access_ids.filter(auth_type="biometric"):
            mapping = access_id_entry.mapping
            access_decision = get_access_decision(access_id_entry.access_id, mapping, settings)
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


def get_access_decision(device_access_id, mapping, settings):
    """Return access decision based on the authorization flow setting and relevant balance thresholds if applicable"""
    
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
                access_id_entry = AccessId.objects.select_related('mapping').filter(access_id=card_uid).first()
                mapping = access_id_entry.mapping if access_id_entry else None

                # map wiegand id to the same user if there is a mapping for the card uid and grand access, otherwise reject access
                if access_id_entry and mapping and wiegand_id:
                    AccessId.objects.update_or_create(access_id=wiegand_id, defaults={
                        "auth_type": "biometric",
                        "mapping": mapping
                        }
                    )
                    _log_access_event(wiegand_id, "grant", mapping, access_type="one_step_mapping", access_control_device=access_control_device if access_control_device else None)
                    print("Access GRANTED for wiegand_id:", wiegand_id)  # Debug log
                    return JsonResponse({"access": "GRANT"}, status=HTTPStatus.OK)
                else:
                    if wiegand_id:
                        _log_access_event(wiegand_id, "reject", mapping, access_type="one_step_mapping", access_control_device=access_control_device if access_control_device else None)
                        print("Access REJECTED for wiegand_id:", wiegand_id)  # Debug log
                    else:
                        print("Wiegand ID not provided in device_access_id, cannot process mapping. device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "REJECT"}, status=HTTPStatus.OK)

            # normal access event flow for devices without wiegand/card uid mapping scenario
            access_id_entry = AccessId.objects.select_related('mapping').filter(access_id=device_access_id).first()
            mapping = access_id_entry.mapping if access_id_entry else None

            if device_access_id in STAFF_USER_IDS:
                print("Device_access_id is in exceptions list, granting access for testing purposes. device_access_id:", device_access_id)  # Debug log
                _log_access_event(device_access_id, "grant", access_control_device=access_control_device if access_control_device else None)
                return JsonResponse({"access": "GRANT"}, status=HTTPStatus.OK)

            if not mapping:
                print("No mapping found for device_access_id:", device_access_id)  # Debug log
                _log_access_event(device_access_id, "reject", access_control_device=access_control_device if access_control_device else None)

                return JsonResponse({"access": "REJECT"})
                # return JsonResponse({"access": "GRANT"}) # TEMPORARILY ALLOW FOR NOW, TO RETURN TO REJECT ONCE DONE
            settings = Setting.get_solo()

            # continue with access decision logic based on the authorization flow setting and relevant balance thresholds if applicable
            access_decision = next((decision.get("access") for decision in access_decisions_list if decision.get("device_access_id") == device_access_id), "REJECT")

            if access_decision == "GRANT":
                print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
            else:
                print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                access_decision = get_access_decision(device_access_id, mapping, settings)  # fetch fresh access decision based on the latest user data from the external system at the time of the access event in case there have been changes since the last fetch to ensure accurate access decisions based on the most up-to-date user data

            _log_access_event(device_access_id, access_decision.lower(), mapping, access_control_device=access_control_device if access_control_device else None)
            return JsonResponse({"access": access_decision}, status=HTTPStatus.OK)
                                
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        
    elif request.method == "GET":
        # return list of all mappings against access decision for local device caching purposes at the access control device level to allow it to make access decisions even if there are transient issues with the connection to the server or external accounting system.

        if access_decisions_list:
            print("Using cached access decisions list for GET request to access_event_view:", access_decisions_list)  # Debug log to verify that cached access decisions list is being used
        else:
            print("No cached access decisions list available for GET request to access_event_view, fetching fresh access decisions list.")  # Debug log to verify that fresh access decisions list is being fetched due to cache miss

            access_decisions_list = get_access_decisions_list()  # fetch fresh access decisions list to ensure we are providing access control device with the necessary data to make access decisions at the time of the access event in case there have been changes since the last fetch to ensure accurate access decisions based on the most up-to-date user data

        computed_access_decisions_list = []
        # add all staff and card access decisions to the computed access decisions list for local device caching purposes at the access control device level
        for category in access_decisions_list:
            if category.get("staff"):
                computed_access_decisions_list.extend(category.get("staff", []))
            elif category.get("card"):
                computed_access_decisions_list.extend(category.get("card", []))
            elif device_serial_number and category.get(device_serial_number):
                computed_access_decisions_list.extend(category.get(device_serial_number, []))

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


@csrf_exempt
def api_account_mapping_view(request, filter_name=None):
    """API endpoint to return list of mappings based on filter_name (e.g. pending, update) or to create/update/delete mapping based on request method and filter_name (e.g. account_user_id for delete)"""
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


def handle_create_mapping(request, filter_name=None):
    """Handle POST request to create/update mapping between access control user and accounting system user"""
    if filter_name == "update":
        return fetch_external_users()  # Trigger fetch from external system to update user data before updating mapping
    
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

        AccessId.objects.update_or_create(access_id=device_access_id, defaults={"mapping": mapping})
        PendingAccountMapping.objects.all().delete()
        return JsonResponse({"status": "created"}, status=HTTPStatus.CREATED)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST)


def handle_delete_mapping(request, filter_name=None):
    """Handle DELETE request to remove mapping between access control user and accounting system user (set device_access_id to null for the given account_user_id)"""
    if filter_name is None:
        return JsonResponse({"error": "account_user_id is required for DELETE request"}, status=HTTPStatus.BAD_REQUEST) 

    mapping = AccountMapping.objects.filter(account_user_id=filter_name).first()
    if not mapping:
        return JsonResponse({"error": "No mapping found for that account user."}, status=HTTPStatus.NOT_FOUND)

    mapping.access_ids.all().delete()
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
            mapping, created = AccountMapping.objects.update_or_create(
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
            # print(f"Account number: {user.get('accountNumber')}. {'Created' if created else 'Updated'} AccountMapping for account_user_id {mapping.account_user_id} with first_name: {mapping.first_name}, last_name: {mapping.last_name}, narration: {mapping.narration}, usd_balance: {mapping.usd_balance}, zwg_balance: {mapping.zwg_balance}")  # Debug log to verify creation or update of AccountMapping entry for each user from external system

            # log creation or update of AccountMapping entry for each user from external system to file
            with open("mapping_log.txt", "a") as f:
                f.write(f"Account number: {user.get('accountNumber')}. {'Created' if created else 'Updated'} AccountMapping for account_user_id {mapping.account_user_id} with first_name: {mapping.first_name}, last_name: {mapping.last_name}, narration: {mapping.narration}, usd_balance: {mapping.usd_balance}, zwg_balance: {mapping.zwg_balance} at {timezone.now()}\n")

            # create or update AccessId entry
            access, _ = AccessId.objects.update_or_create(
                access_id=str(user.get('cardId')),
                defaults={
                    "auth_type": "card",  # Assuming auth_type is card for all users from the external system, but in a real implementation we would want to ensure we are capturing the correct auth_type for each user based on the data from the external system
                    "mapping": mapping
                }
            )

            # log creation or update of AccessId entry for each user from external system to file
            with open("mapping_log.txt", "a") as f:
                f.write(f"Account number: {user.get('accountNumber')}. {'Created' if _ else 'Updated'} AccessId for access_id {access.access_id} with auth_type: {access.auth_type} mapped to account_user_id {mapping.account_user_id} at {timezone.now()}\n")

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

    
@csrf_exempt
def set_modal_state_view(request):
    """API endpoint to set the state of the mapping modal (open/closed)"""
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
