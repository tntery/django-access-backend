import random
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from access.models import AccountMapping, MappingModalState
from access.management.seed_data import INITIAL_ACCOUNT_MAPPINGS

EXTERNAL_ACCOUNTING_DB_NAME = 'external_accounting.sqlite3'

# Seed data used to mimic records coming from an external accounting system.
SEED_ACCOUNTING_USERS = [
    {'account_user_id': '904738', 'full_name': 'Youssef Diallo', 'balance': -7.53},
    {'account_user_id': '984768', 'full_name': 'Moussa Diallo', 'balance': -5.99},
    {'account_user_id': '199568', 'full_name': 'Chioma Osei', 'balance': 7.7},
    {'account_user_id': '955902', 'full_name': 'Nia Mensah', 'balance': -2.55},
    {'account_user_id': '424644', 'full_name': 'Bamidele Toure', 'balance': 0.76},
    {'account_user_id': '105832', 'full_name': 'Zuberi Kalu', 'balance': 2.41},
    {'account_user_id': '739210', 'full_name': 'Amara Dlamini', 'balance': -1.18},
    {'account_user_id': '552109', 'full_name': 'Tendai Bekele', 'balance': 9.87},
    {'account_user_id': '883201', 'full_name': 'Kofi Okonkwo', 'balance': -4.32},
    {'account_user_id': '664903', 'full_name': 'Fatoumata Keita', 'balance': 6.54},
    {'account_user_id': '221094', 'full_name': 'Kwame Gbeho', 'balance': -9.91},
    {'account_user_id': '334812', 'full_name': 'Lerato Sow', 'balance': 0.05},
    {'account_user_id': '445723', 'full_name': 'Oluchi Chineke', 'balance': 3.22},
    {'account_user_id': '990123', 'full_name': 'Tariro Moyo', 'balance': -8.76},
    {'account_user_id': '123456', 'full_name': 'Zanele Luthuli', 'balance': 5.43},
]

class Command(BaseCommand):
    help = 'Create a mock external accounting SQLite DB and bootstrap initial account mappings from it'

    def _external_db_path(self) -> Path:
        return Path(settings.BASE_DIR) / EXTERNAL_ACCOUNTING_DB_NAME

    def _seed_external_accounting_db(self, db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DROP TABLE IF EXISTS accounting_users')
            cursor.execute(
                '''
                CREATE TABLE accounting_users (
                    account_user_id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    balance REAL NOT NULL
                )
                '''
            )
            cursor.executemany(
                '''
                INSERT INTO accounting_users (
                    account_user_id,
                    full_name,
                    balance
                ) VALUES (?, ?, ?)
                ''',
                [
                    (
                        user['account_user_id'],
                        user['full_name'],
                        user['balance'],
                    )
                    for user in SEED_ACCOUNTING_USERS
                ],
            )
            conn.commit()

    def _read_external_accounting_users(self, db_path: Path) -> list[str]:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT account_user_id
                FROM accounting_users
                ORDER BY account_user_id
                '''
            )
            return [row[0] for row in cursor.fetchall()]

    def handle(self, *args, **options):
        external_db_path = self._external_db_path()
        self._seed_external_accounting_db(external_db_path)
        external_users = self._read_external_accounting_users(external_db_path)

        accounting_systems = ['palladium', 'quickbooks', 'xero']
        for account_user_id in external_users:
            AccountMapping.objects.update_or_create(
                account_user_id=str(account_user_id),
                defaults={
                    'device_access_id': INITIAL_ACCOUNT_MAPPINGS.get(str(account_user_id)),
                    'accounting_system': random.choice(accounting_systems),
                },
            )

        # Ensure MappingModalState record exists
        MappingModalState.objects.get_or_create(id=1, defaults={'state': 'closed'})

        # Output success message
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully loaded {len(external_users)} mappings from external DB at {external_db_path}'
            )
        )