import json
import sqlite3
import environ
import requests
from requests.exceptions import RequestException, HTTPError
from http import HTTPStatus
from contextlib import closing
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from .models import AccessEventLog, AccountMapping, PendingAccountMapping, MappingModalState, Setting
from .forms import SettingForm

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'

env = environ.Env()
environ.Env.read_env()
EXTERNAL_ACCOUNTING_URL = env('PALADIUM_API_URL')


def _log_access_event(device_access_id, access_status, mapping=None):
    """Persist access decisions for auditing and troubleshooting."""
    AccessEventLog.objects.create(
        device_access_id=str(device_access_id) if device_access_id is not None else '',
        account_user_id=mapping.account_user_id if mapping else None,
        accounting_system=mapping.accounting_system if mapping else 'palladium',
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


def get_palladium_balance(device_access_id):
    """Return a balance from the external accounting data for the mapped device_access_id."""
    # Pull current users from the external accounting DB and locate the mapped user balance.
    for user in get_test_users():
        if str(user['device_access_id']) == str(device_access_id):
            print(f"Found user for device_access_id {device_access_id}: {user['full_name']} with balance {user['balance']}")  # Debug log
            return user['balance']


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

            settings = Setting.get_solo()

            match settings.authorization_flow:
                case "grant_all":
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'grant', mapping)
                    return JsonResponse({"access": "GRANT"})
                case "reject_all":
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'reject', mapping)
                    return JsonResponse({"access": "REJECT"})
                case _:
            
                    # Normal flow: check mapping
                    if mapping:
                        balance = get_palladium_balance(mapping.device_access_id)
                        print(f"Access ID: {device_access_id}, Palladium ID: {mapping.device_access_id}, Balance: {balance}")  # Debug log
                        if balance is not None and balance >= settings.balance_threshold:
                            print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                            _log_access_event(device_access_id, 'grant', mapping)
                            return JsonResponse({"access": "GRANT"})
                        
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    _log_access_event(device_access_id, 'reject', mapping)
                    return JsonResponse({"access": "REJECT"})
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)


def account_mapping_list_view(request):
    """Return a list of all mappings of the access control user to the different external system accounts for display in the admin interface"""

    # User data is read from the mock external accounting SQLite DB and merged with current mapping state.
    # TODO : Add filtering and pagination as needed for real implementation when pulling from actual database with potentially large number of users.

    user_list = AccountMapping.objects.all().order_by('-last_name', '-first_name')
    last_updated = AccountMapping.objects.order_by('-last_updated_at').first()
    print(f"Last updated mapping: {last_updated}")  # Debug log to verify mapping
    print(f"User list for mapping view: {user_list}")  # Debug log to verify user list
    return render(request, "access/mapping_list.html", {"users": user_list, "last_updated": last_updated.last_updated_at if last_updated else "-"})


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
