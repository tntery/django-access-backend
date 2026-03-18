import psycopg2
import random
import json
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from .models import Mapping, TempMapping, ModalStatus

test_users = [
    {'mapping_id': 904738, 'full_name': 'Youssef Diallo', 'balance': -7.53, 'access_id': 175750, 'connected': True},
    {'mapping_id': 984768, 'full_name': 'Moussa Diallo', 'balance': -5.99, 'access_id': 979017, 'connected': True},
    {'mapping_id': 199568, 'full_name': 'Chioma Osei', 'balance': 7.7, 'access_id': 123456, 'connected': False},
    {'mapping_id': 955902, 'full_name': 'Nia Mensah', 'balance': -2.55, 'access_id': None, 'connected': True},
    {'mapping_id': 424644, 'full_name': 'Bamidele Toure', 'balance': 0.76, 'access_id': 968538, 'connected': True},
    {'mapping_id': 105832, 'full_name': 'Zuberi Kalu', 'balance': 2.41, 'access_id': 648219, 'connected': False},
    {'mapping_id': 739210, 'full_name': 'Amara Dlamini', 'balance': -1.18, 'access_id': None, 'connected': True},
    {'mapping_id': 552109, 'full_name': 'Tendai Bekele', 'balance': 9.87, 'access_id': 443210, 'connected': False},
    {'mapping_id': 883201, 'full_name': 'Kofi Okonkwo', 'balance': -4.32, 'access_id': 129034, 'connected': True},
    {'mapping_id': 664903, 'full_name': 'Fatoumata Keita', 'balance': 6.54, 'access_id': None, 'connected': False},
    {'mapping_id': 221094, 'full_name': 'Kwame Gbeho', 'balance': -9.91, 'access_id': 882019, 'connected': True},
    {'mapping_id': 334812, 'full_name': 'Lerato Sow', 'balance': 0.05, 'access_id': 551029, 'connected': True},
    {'mapping_id': 445723, 'full_name': 'Oluchi Chineke', 'balance': 3.22, 'access_id': None, 'connected': False},
    {'mapping_id': 990123, 'full_name': 'Tariro Moyo', 'balance': -8.76, 'access_id': 771023, 'connected': True},
    {'mapping_id': 123456, 'full_name': 'Zanele Luthuli', 'balance': 5.43, 'access_id': 662019, 'connected': False},
] # Simulated user data for testing

def get_palladium_balance(access_id):
    """Return a random balance for the given access_id (replace with actual DB query)"""
    return random.randint(-10, 10)  # Simulate balance for testing

@csrf_exempt
def access_event(request):
    """Handle access event from access control device"""
    if request.method == "POST":

        try:

            print("Received access event:", request.body)  # Debug log

            # Parse the incoming JSON data
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log
            
            # get access_id from JSON data
            access_id = data.get("access_id")
            print("Extracted access_id:", access_id)  # Debug log

            modal_status = ModalStatus.objects.first()
            if modal_status and modal_status.status == "open":
                # Insert/update TempMapping with latest access_id
                TempMapping.objects.update_or_create(id=1, defaults={"access_id": access_id})
                return JsonResponse({"access": "REJECT"})  # always reject while mapping modal is open

            # Normal flow: check mapping
            mapping = Mapping.objects.filter(access_id=access_id).first()
            if mapping:
                balance = get_palladium_balance(mapping.access_id)
                print(f"Access ID: {access_id}, Palladium ID: {mapping.access_id}, Balance: {balance}")  # Debug log
                if balance is not None and balance >= 0:
                    print("Access GRANTED for access_id:", access_id)  # Debug log
                    return JsonResponse({"access": "GRANT"})
                
            print("Access REJECTED for access_id:", access_id)  # Debug log
            return JsonResponse({"access": "REJECT"})
            
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)

def mapping_list(request):
    """Return a list of all mappings for display in the admin interface"""

    return render(request, "access/mapping_list.html", {"users": test_users})

@csrf_exempt
def set_modal_status(request):
    if request.method == "POST":
        print("Received modal status update:", request.body)  # Debug log
        try:
            data = json.loads(request.body)
            print("JSON data:", data)  # Debug log

            status = data.get("status")
            print("Extracted status:", status)  # Debug log

            ModalStatus.objects.update_or_create(id=1, defaults={"status": status})

            if status == "closed":
                TempMapping.objects.all().delete()

            return JsonResponse({"status": "created"} , status=201)
        
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
    else:
        return JsonResponse({"error": "Invalid request method"}, status=405)
    
def poll_temp_mapping(request):
    temp = TempMapping.objects.first()
    return JsonResponse({"access_id": temp.access_id if temp else None})

@csrf_exempt
def confirm_mapping(request):
    return JsonResponse({"status": "success"})