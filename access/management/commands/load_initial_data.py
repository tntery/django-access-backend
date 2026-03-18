import random
from django.core.management.base import BaseCommand
from access.views import test_users
from access.models import Mapping, ModalStatus

class Command(BaseCommand):
    help = 'Load initial mapping data into the database (for testing purposes)'

    def handle(self, *args, **options):
        # In a real implementation, this would pull from the actual accounting system database to create the initial mappings. For testing, we will randomly assign access_ids to some of the test users and randomly assign mapping systems.
        mapping_systems = ["Palladium", "QuickBooks", "Xero"]
        for user in test_users:
            Mapping.objects.update_or_create(
                mapping_id=str(user['mapping_id']),
                defaults={
                    "access_id": str(user['access_id']) if user['access_id'] else None,
                    "mapping_system": str(random.choice(mapping_systems)).lower()
                }
            )

        # Ensure ModalStatus record exists
        ModalStatus.objects.get_or_create(id=1, defaults={"status": "closed"})

        # Output success message
        self.stdout.write(self.style.SUCCESS('Successfully loaded initial data'))