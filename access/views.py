import psycopg2
import random
import json
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from .models import AccountMapping, PendingAccountMapping, MappingModalState

test_users = [
    {'account_user_id': 904738, 'full_name': 'Youssef Diallo', 'balance': -7.53, 'device_access_id': 175750, 'connected': True},
    {'account_user_id': 984768, 'full_name': 'Moussa Diallo', 'balance': -5.99, 'device_access_id': 979017, 'connected': True},
    {'account_user_id': 199568, 'full_name': 'Chioma Osei', 'balance': 7.7, 'device_access_id': 123456, 'connected': False},
    {'account_user_id': 955902, 'full_name': 'Nia Mensah', 'balance': -2.55, 'device_access_id': None, 'connected': True},
    {'account_user_id': 424644, 'full_name': 'Bamidele Toure', 'balance': 0.76, 'device_access_id': 968538, 'connected': True},
    {'account_user_id': 105832, 'full_name': 'Zuberi Kalu', 'balance': 2.41, 'device_access_id': 648219, 'connected': False},
    {'account_user_id': 739210, 'full_name': 'Amara Dlamini', 'balance': -1.18, 'device_access_id': None, 'connected': True},
    {'account_user_id': 552109, 'full_name': 'Tendai Bekele', 'balance': 9.87, 'device_access_id': 443210, 'connected': False},
    {'account_user_id': 883201, 'full_name': 'Kofi Okonkwo', 'balance': -4.32, 'device_access_id': 129034, 'connected': True},
    {'account_user_id': 664903, 'full_name': 'Fatoumata Keita', 'balance': 6.54, 'device_access_id': None, 'connected': False},
    {'account_user_id': 221094, 'full_name': 'Kwame Gbeho', 'balance': -9.91, 'device_access_id': 882019, 'connected': True},
    {'account_user_id': 334812, 'full_name': 'Lerato Sow', 'balance': 0.05, 'device_access_id': 551029, 'connected': True},
    {'account_user_id': 445723, 'full_name': 'Oluchi Chineke', 'balance': 3.22, 'device_access_id': None, 'connected': False},
    {'account_user_id': 990123, 'full_name': 'Tariro Moyo', 'balance': -8.76, 'device_access_id': 771023, 'connected': True},
    {'account_user_id': 123456, 'full_name': 'Zanele Luthuli', 'balance': 5.43, 'device_access_id': 662019, 'connected': False},
] # Simulated user data for testing

def get_test_users():
    """Simulate fetching user data from the accounting system database (replace with actual DB query)"""
    # for mapping in AccountMapping.objects.all(): get the corresponding user from test_users and update the device_access_id and connected status based on the mapping. connected = True if mapping exists and device_access_id is not None, otherwise False. This simulates the state of the users based on the current mappings in the database.
    for mapping in AccountMapping.objects.all():
        for user in test_users:
            if str(user['account_user_id']) == mapping.account_user_id:
                user['device_access_id'] = mapping.device_access_id
                user['connected'] = True if mapping.device_access_id else False
        
    return test_users

def get_palladium_balance(device_access_id):
    """Return a random balance for the given device_access_id (replace with actual DB query)"""
    # from get_test_users(), find the user with the matching device_access_id and return their balance. This simulates fetching the balance from the accounting system based on the mapping.
    for user in get_test_users():
        if str(user['device_access_id']) == str(device_access_id):
            print(f"Found user for device_access_id {device_access_id}: {user['full_name']} with balance {user['balance']}")  # Debug log
            return user['balance']
        
@csrf_exempt
def access_event(request):
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

            # Normal flow: check mapping
            mapping = AccountMapping.objects.filter(device_access_id=device_access_id).first()
            if mapping:
                balance = get_palladium_balance(mapping.device_access_id)
                print(f"Access ID: {device_access_id}, Palladium ID: {mapping.device_access_id}, Balance: {balance}")  # Debug log
                if balance is not None and balance >= 0:
                    print("Access GRANTED for device_access_id:", device_access_id)  # Debug log
                    return JsonResponse({"access": "GRANT"})
                
            print("Access REJECTED for device_access_id:", device_access_id)  # Debug log
            return JsonResponse({"access": "REJECT"})
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)

def account_mapping_list(request):
    """Return a list of all mappings of the access control user to the different external system accounts for display in the admin interface"""

    return render(request, "access/mapping_list.html", {"users": get_test_users()})

@csrf_exempt
def set_modal_state(request):
    if request.method == "POST":
        print("Received modal state update:", request.body)  # Debug log
        try:
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log

            state = data.get("state")
            print("Extracted state:", state)  # Debug log

            MappingModalState.objects.update_or_create(id=1, defaults={"state": state})

            if state == "closed":
                PendingAccountMapping.objects.all().delete()

            return JsonResponse({"status": "created"} , status=201)
        
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
def poll_temp_mapping(request):
    temp = PendingAccountMapping.objects.first()
    return JsonResponse({"device_access_id": temp.device_access_id if temp else None})

@csrf_exempt
def confirm_mapping(request):
    if request.method == "POST":

        try:
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log

            device_access_id = data.get("device_access_id")
            account_user_id = data.get("account_user_id")
            print("Extracted data:", device_access_id, account_user_id)  # Debug log

            AccountMapping.objects.update_or_create(device_access_id=device_access_id, defaults={"account_user_id": account_user_id})
            PendingAccountMapping.objects.all().delete()
            return JsonResponse({"status": "created"}, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)