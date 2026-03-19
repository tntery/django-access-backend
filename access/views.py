import json
import sqlite3
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from .models import AccountMapping, PendingAccountMapping, MappingModalState, Setting
from .forms import SettingForm

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'

def get_test_users():
    """Fetch users from the mock external accounting DB and merge mapping state."""
    external_db_path = Path(settings.BASE_DIR) / EXTERNAL_ACCOUNTING_DB_NAME
    if not external_db_path.exists():
        return []

    try:
        with sqlite3.connect(external_db_path) as conn:
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

            modal_state = MappingModalState.objects.first()
            if modal_state and modal_state.state == "open":
                # Insert/update PendingAccountMapping with latest device_access_id
                PendingAccountMapping.objects.update_or_create(id=1, defaults={"device_access_id": device_access_id})
                return JsonResponse({"access": "REJECT"})  # always reject while mapping modal is open

            settings = Setting.get_solo()

            match settings.authorization_flow:
                case "grant_all":
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "GRANT"})
                case "reject_all":
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "REJECT"})
                case _:
            
                    # Normal flow: check mapping
                    mapping = AccountMapping.objects.filter(device_access_id=device_access_id).first()
                    if mapping:
                        balance = get_palladium_balance(mapping.device_access_id)
                        print(f"Access ID: {device_access_id}, Palladium ID: {mapping.device_access_id}, Balance: {balance}")  # Debug log
                        if balance is not None and balance >= settings.balance_threshold:
                            print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                            return JsonResponse({"access": "GRANT"})
                        
                    print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "REJECT"})
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)


def account_mapping_list_view(request):
    """Return a list of all mappings of the access control user to the different external system accounts for display in the admin interface"""

    # User data is read from the mock external accounting SQLite DB and merged with current mapping state.
    # TODO : Add filtering and pagination as needed for real implementation when pulling from actual database with potentially large number of users.
    return render(request, "access/mapping_list.html", {"users": get_test_users()})


@csrf_exempt
def api_account_mapping_view(request, account_user_id=None):
    """API endpoint to return list of mappings based on account_user_id filter (e.g. pending)"""
    match request.method:
        case "GET":
            return handle_get_pending_mapping(request, account_user_id)
        case "POST":
            return handle_create_mapping(request)
        case "DELETE":
            return handle_delete_mapping(request, account_user_id)
        case _:
            return JsonResponse({"error": "Invalid request method"}, status=405)


def handle_get_pending_mapping(request, account_user_id=None):
    """Handle GET request to return pending mapping or all mappings based on account_user_id filter"""
    if account_user_id is None:
        # return message that account_user_id filter is required for GET requests
        return JsonResponse({"error": "account_user_id filter is required for GET request"}, status=400)
    else:
        if account_user_id == "pending":
            temp = PendingAccountMapping.objects.first()
            return JsonResponse({"device_access_id": temp.device_access_id if temp else None})
        else:
            # return message that only pending filter is supported for GET requests
            return JsonResponse({"error": "Invalid filter for GET request. Only 'pending' is supported."}, status=400)


def handle_create_mapping(request):
    """Handle POST request to create/update mapping between access control user and accounting system user"""
    try:
        data = json.loads(request.body)
        print("JSON data:", data)  # Debug log

        device_access_id = data.get("device_access_id")
        account_user_id = data.get("account_user_id")
        print("Extracted data:", device_access_id, account_user_id)  # Debug log

        if AccountMapping.objects.filter(device_access_id=device_access_id).exists():
            return JsonResponse({"error": "This device's Access ID is already mapped to another account"}, status=400)

        AccountMapping.objects.update_or_create( account_user_id=account_user_id, defaults={"device_access_id": device_access_id})
        PendingAccountMapping.objects.all().delete()
        return JsonResponse({"status": "created"}, status=201)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)


def handle_delete_mapping(request, account_user_id=None):
    """Handle DELETE request to remove mapping between access control user and accounting system user (set device_access_id to null for the given account_user_id)"""
    if account_user_id is None:
        return JsonResponse({"error": "account_user_id filter is required for DELETE request"}, status=400) 

    mapping = AccountMapping.objects.filter(account_user_id=account_user_id).first()
    if not mapping:
        return JsonResponse({"error": "No mapping found for that account user."}, status=404)

    mapping.device_access_id = None
    mapping.save()
    return JsonResponse({"status": "unmapped"}, status=200)

    
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

            return JsonResponse({"status": "created"} , status=201)
        
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)
