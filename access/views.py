import json
import sqlite3
import environ
import requests
from requests.exceptions import RequestException, HTTPError
from http import HTTPStatus
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from .models import AccessEventLog, AccountMapping, PendingAccountMapping, MappingModalState, Setting
from .forms import SettingForm

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'
ACCOUNT_MAPPING_PAGE_SIZE = 25

env = environ.Env()
environ.Env.read_env()
EXTERNAL_ACCOUNTING_URL = env('PALADIUM_API_URL')


def _log_access_event(device_access_id, access_status, mapping=None):
    """Persist access decisions for auditing and troubleshooting."""
    AccessEventLog.objects.create(
        device_access_id=str(device_access_id) if device_access_id is not None else '',
        user=mapping if mapping else None,
        access_status=access_status,
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
        for mapping in AccountMapping.objects.all()
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


def account_mapping_list_view(request):
    """Return a list of all mappings of the access control user to the different external system accounts for display in the admin interface"""

    # User data is read from the mock external accounting SQLite DB and merged with current mapping state.
    # TODO : Add filtering and pagination as needed for real implementation when pulling from actual database with potentially large number of users.

    user_list = AccountMapping.objects.all().order_by('-last_name', '-first_name')
    paginator = Paginator(user_list, ACCOUNT_MAPPING_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))
    last_updated = AccountMapping.objects.order_by('-last_updated_at').first()
    print(f"Last updated time: {last_updated.last_updated_at if last_updated else '-'}")  # Debug log to verify mapping
    # print(f"User list for mapping view: {user_list}")  # Debug log to verify user list
    return render(
        request,
        "access/mapping_list.html",
        {
            "users": page_obj,
            "page_obj": page_obj,
            "last_updated": last_updated.last_updated_at if last_updated else "-",
        },
    )


def settings_view(request):
    """View to display and update settings"""
    settings_obj = Setting.get_solo()
    
    if request.method == 'POST':
        form = SettingForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            return render(request, 'access/settings.html', {'form': form, 'message': 'Settings updated successfully!'})
    else:
        form = SettingForm(instance=settings_obj)
    
    return render(request, 'access/settings.html', {'form': form})


def get_accounting_balances(device_access_id, currency=None):
    """Return usd and/or zwg balances from the external accounting data for the mapped device_access_id."""
    # get user id from mapping and then get balance for that user from the external accounting system
    mapping = AccountMapping.objects.filter(device_access_id=device_access_id).first()
    if not mapping:
        return None
    try:
        # r = requests.get(f'{EXTERNAL_ACCOUNTING_URL}/api/UserData/get-data-by-cardId/{mapping.account_user_id}', timeout=10)
        # r.raise_for_status()
        # user_data = r.json()

        # get from database for testing purposes since we are not currently updating balances in the AccountMapping table with real-time data from the external system in this demo implementation, but in a real implementation we would want to ensure we are fetching the latest balance data from the external system at the time of the access event to make accurate access decisions based on the most up-to-date user data
        user_data = AccountMapping.objects.filter(device_access_id=device_access_id).first()
        print(f"Fetched user data for device_access_id {device_access_id} from external accounting system:", user_data)  # Debug log to verify response from external system
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


@csrf_exempt
def access_event_view(request):
    """Handle access event from access control device"""
    if request.method == "POST":

        try:

            print("Received access event:", request.body)  # Debug log

            # Parse the incoming JSON data
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log
            
            # get device_access_id from JSON data
            device_access_id = data.get("access_id") # could be a user_id or card_id depending on the access control system
            print("Extracted device_access_id:", device_access_id)  # Debug log

            mapping = AccountMapping.objects.filter(device_access_id=device_access_id).first()

            modal_state = MappingModalState.objects.first()
            if modal_state and modal_state.state == "open":
                # Insert/update PendingAccountMapping with latest device_access_id
                PendingAccountMapping.objects.update_or_create(id=1, defaults={"device_access_id": device_access_id})
                _log_access_event(device_access_id, 'reject', mapping)
                return JsonResponse({"access": "REJECT"})  # always reject while mapping modal is open

            if not mapping:
                print("No mapping found for device_access_id:", device_access_id)  # Debug log
                _log_access_event(device_access_id, 'reject', mapping)
                return JsonResponse({"access": "REJECT"})
            settings = Setting.get_solo()

            # continue with access decision logic based on the authorization flow setting and relevant balance thresholds if applicable

            authorization_flow = settings.authorization_flow

            match authorization_flow:
                case "grant_all":
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'grant', mapping)
                    return JsonResponse({"access": "GRANT"})
                case "reject_all":
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'reject', mapping)
                    return JsonResponse({"access": "REJECT"})
                case "check_usd_balance" | "check_zwg_balance":
                    
                    filter_currency = 'usd' if authorization_flow == "check_usd_balance" else 'zwg'

                    if getattr(mapping, f'{filter_currency}_balance') is not None and getattr(mapping, f'{filter_currency}_balance') >= getattr(settings, f'{filter_currency}_balance_threshold'):
                        # grant access if user meets balance threshold
                        print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                        _log_access_event(device_access_id, 'grant', mapping)
                        return JsonResponse({"access": "GRANT"})
                    else:
                        # fetch latest balance from external system in case it has changed since last fetch and check again before rejecting access
                        current_balance = get_accounting_balances(mapping.device_access_id, currency=filter_currency.upper())

                        print(f"Access ID: {device_access_id}, Palladium ID: {mapping.device_access_id}, {filter_currency.upper()} balance: {current_balance}")  # Debug log

                        if current_balance is None:
                            # if there was an error fetching balance data from the external system, default to granting access to avoid locking out users due to transient issues with the external system, but log the event for troubleshooting
                            print("Error fetching balance data for device_access_id:", device_access_id, "Defaulting to GRANT access but check logs for troubleshooting.")  # Debug log
                            _log_access_event(device_access_id, 'grant', mapping)
                            return JsonResponse({"access": "GRANT"})
                        elif current_balance >= getattr(settings, f'{filter_currency}_balance_threshold'):
                            print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                            
                            _log_access_event(device_access_id, 'grant', mapping)
                            return JsonResponse({"access": "GRANT"})
                        else:
                            print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                            _log_access_event(device_access_id, 'reject', mapping)
                            return JsonResponse({"access": "REJECT"})
                case "check_usd_or_zwg_balance":
                    # check if user meets either balance threshold to grant access
                    if ((mapping.usd_balance is not None and mapping.usd_balance >= settings.usd_balance_threshold) or 
                        (mapping.zwg_balance is not None and mapping.zwg_balance >= settings.zwg_balance_threshold)):
                        print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                        _log_access_event(device_access_id, 'grant', mapping)
                        return JsonResponse({"access": "GRANT"})
                    else:
                        # fetch latest balances from external system in case they have changed since last fetch and check again before rejecting access
                        current_balances = get_accounting_balances(mapping.device_access_id)

                        print(f"Access ID: {device_access_id}, Palladium ID: {mapping.device_access_id}, Current balances: {current_balances}")  # Debug log

                        if current_balances is None:
                            # if there was an error fetching balance data from the external system, default to granting access to avoid locking out users due to transient issues with the external system, but log the event for troubleshooting
                            print("Error fetching balance data for device_access_id:", device_access_id, "Defaulting to GRANT access but check logs for troubleshooting.")  # Debug log
                            _log_access_event(device_access_id, 'grant', mapping)
                            return JsonResponse({"access": "GRANT"})
                        elif ((current_balances.get('usd_balance') is not None and current_balances.get('usd_balance') >= settings.usd_balance_threshold) or 
                              (current_balances.get('zwg_balance') is not None and current_balances.get('zwg_balance') >= settings.zwg_balance_threshold)):
                            print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                            _log_access_event(device_access_id, 'grant', mapping)
                            return JsonResponse({"access": "GRANT"})
                        else:
                            print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                            _log_access_event(device_access_id, 'reject', mapping)
                            return JsonResponse({"access": "REJECT"})
                case _:
                    print("Invalid authorization flow setting. Defaulting to REJECT access for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'reject', mapping)
                    return JsonResponse({"access": "REJECT"})
                                
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

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

        if AccountMapping.objects.filter(device_access_id=device_access_id).exists():
            return JsonResponse({"error": "This device's Access ID is already mapped to another account"}, status=HTTPStatus.BAD_REQUEST)

        AccountMapping.objects.update_or_create( account_user_id=account_user_id, defaults={"device_access_id": device_access_id})
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

    mapping.device_access_id = None
    mapping.save()
    return JsonResponse({"status": "unmapped"}, status=HTTPStatus.NO_CONTENT)


def fetch_external_users():
    """API endpoint to fetch users from the external accounting system and update the AccountMapping table with any new users or updated user info (e.g. balance)"""
        
    try:
        r = requests.get(f'{EXTERNAL_ACCOUNTING_URL}/api/UserData/get-data', timeout=15)
        r.raise_for_status()
        users = r.json()
        print(f"Fetched {len(users)} users from external accounting system:", users)  # Debug log to verify response from external system 

        # update database with any new users from the external system that are not already in the AccountMapping table
        AccountMapping.objects.bulk_create(
            [
                AccountMapping(
                    account_user_id=str(user.get('cardId')), # Assuming cardId is the unique identifier for the user in the external system 
                    first_name=user.get('firstName', ''),
                    last_name=user.get('lastName', ''),
                    usd_balance=user.get('usdBalance', -9999999.00),
                    zwg_balance=user.get('zwdBalance', -9999999.00),
                    last_updated_at=timezone.now(),
                ) for user in users
            ],
            update_conflicts=True,
            unique_fields=['account_user_id',],
            update_fields=['first_name', 'last_name', 'usd_balance', 'zwg_balance']
        )

        return JsonResponse({"status": "external users updated"}, status=HTTPStatus.OK)
    except HTTPError as e:
        print("HTTP error occurred while fetching users from external accounting system:", e)  # Debug log for HTTP errors
        return JsonResponse({"error": "Failed to fetch users from external accounting system due to HTTP error"}, status=HTTPStatus.BAD_GATEWAY)
    except RequestException as e:
        print("Error fetching users from external accounting system:", e)  # Debug log for error handling
        return JsonResponse({"error": "Failed to fetch users from external accounting system"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    
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
