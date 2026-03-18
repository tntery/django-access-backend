import random
from django.core.management.base import BaseCommand
from access.views import test_users
from access.models import AccountMapping, MappingModalState

class Command(BaseCommand):
    help = 'Load initial mapping data into the database (for testing purposes)'

    def handle(self, *args, **options):
        # In a real implementation, this would pull from the actual accounting system database to create the initial mappings. For testing, we will randomly assign device_access_ids to some of the test users and randomly assign mapping systems.
        accounting_systems = ["Palladium", "QuickBooks", "Xero"]
        for user in test_users:
            AccountMapping.objects.update_or_create(
                account_user_id=str(user['account_user_id']),
                defaults={
                    "device_access_id": str(user['device_access_id']) if user['device_access_id'] else None,
                    "accounting_system": str(random.choice(accounting_systems)).lower()
                }
            )

        # Ensure MappingModalState record exists
        MappingModalState.objects.get_or_create(id=1, defaults={"state": "closed"})

        # Output success message
        self.stdout.write(self.style.SUCCESS('Successfully loaded initial data'))