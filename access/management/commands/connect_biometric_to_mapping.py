import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from access.models import AccessId, AccountMapping, MappingModalState

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'


class Command(BaseCommand):
    help = 'Connect biometric user ids from a csv file to the corresponding account mappings in the system'

    def handle(self, *args, **options):
        
        # read the csv file and create AccessId entries for each biometric user id, linking to the corresponding AccountMapping if a match is found
        csv_path = Path(settings.BASE_DIR) / 'biometric_user_ids.csv'
        count = 0
        with open(csv_path, 'r') as f:
            reader = csv.reader(f)
            for idx, row in enumerate(reader, start=1):
                if len(row) == 3 and row[2].strip() != "":
                    self.stdout.write(self.style.WARNING(f'Skipping invalid row {idx} in CSV: {row}'))
                    continue

                biometric_user_id, account_user_id = row[:2]
                print(f"Processing row {idx}: biometric_user_id={biometric_user_id}, account_user_id={account_user_id}")
                if not biometric_user_id or not account_user_id:
                    self.stdout.write(self.style.WARNING(f'Skipping row {idx} with missing values in CSV: {row}'))
                    continue

                if account_user_id.lower() == 'staff':
                    continue

                try:
                    mapping = AccountMapping.objects.get(account_user_id=account_user_id)
                    AccessId.objects.update_or_create(
                        access_id=biometric_user_id,
                        defaults={
                            'auth_type': 'biometric',
                            'mapping': mapping,
                        }
                    )
                    self.stdout.write(self.style.SUCCESS(f'Connected biometric user id {biometric_user_id} to account user id {account_user_id}'))
                    count += 1
                except AccountMapping.DoesNotExist:
                    self.stdout.write(self.style.ERROR(f'No AccountMapping found for account user id {account_user_id}, skipping biometric user id {biometric_user_id}'))

        # output success message
        self.stdout.write(self.style.SUCCESS(f'Successfully connected {count} biometric user ids from CSV file'))

        # write to csv successfully connected biometric user ids, their corresponding account user ids and mapping narration for reference
        output_csv_path = Path(settings.BASE_DIR) / 'connected_biometric_user_ids.csv'
        with open(output_csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['biometric_user_id', 'account_user_id', 'mapping_narration'])
            for access_id in AccessId.objects.filter(auth_type='biometric').select_related('mapping'):
                writer.writerow([access_id.access_id, access_id.mapping.account_user_id, access_id.mapping.narration or ''])
        self.stdout.write(self.style.SUCCESS(f'Output successfully connected biometric user ids to {output_csv_path}'))
