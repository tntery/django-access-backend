import psycopg2
import random
import json
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from .models import Mapping, TempMapping, ModalStatus

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
        return JsonResponse({"error": "Invalid request method"}, status=400)