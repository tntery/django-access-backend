import csv
import requests
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from access.models import AccountMapping, AccessId


class Command(BaseCommand):
    help = "Remove AccountMapping and AccessId records not present in API AllMemberBalances"

    def add_arguments(self, parser):
        parser.add_argument(
            "--api-url",
            type=str,
            default="https://squashgate.paradigmuser.com/api/AllMemberBalances",
            help="API endpoint URL (default: AllMemberBalances)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview deletions without modifying database",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip confirmation prompt (use with caution)",
        )
        parser.add_argument(
            "--export-csv",
            type=str,
            default="stale_account_mappings_dry_run.csv",
            help="Filename to export dry-run deletion candidates to CSV",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="API request timeout in seconds (default: 30)",
        )

    def handle(self, *args, **options):
        api_url = options["api_url"]
        dry_run = options["dry_run"]
        force = options["force"]
        export_csv = options.get("export_csv")
        timeout = options["timeout"]

        self.stdout.write(self.style.HTTP_INFO(f"\n📡 Fetching data from API: {api_url}\n"))

        try:
            response = requests.get(api_url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise CommandError(f"API request timed out after {timeout} seconds")
        except requests.exceptions.ConnectionError:
            raise CommandError(f"Failed to connect to API: {api_url}")
        except requests.exceptions.RequestException as e:
            raise CommandError(f"API request failed: {str(e)}")

        try:
            api_data = response.json()
        except ValueError:
            raise CommandError("API response is not valid JSON")

        # Handle both list and dict responses
        if isinstance(api_data, dict):
            if "data" in api_data:
                items = api_data["data"]
            elif "results" in api_data:
                items = api_data["results"]
            elif "items" in api_data:
                items = api_data["items"]
            else:
                items = [api_data]
        elif isinstance(api_data, list):
            items = api_data
        else:
            raise CommandError(f"Unexpected API response format: {type(api_data)}")

        if not items:
            raise CommandError("No data returned by the API")

        # Extract valid accountNumber values from API
        api_account_numbers = set()
        for item in items:
            if isinstance(item, dict) and "accountNumber" in item:
                account_number = str(item["accountNumber"]).strip()
                if account_number:
                    api_account_numbers.add(account_number)

        if not api_account_numbers:
            raise CommandError("No valid accountNumber values found in API response")

        self.stdout.write(f"✓ Loaded {len(items)} items from API")
        self.stdout.write(f"✓ Found {len(api_account_numbers)} unique accountNumber values\n")

        # Find AccountMapping records NOT in the API
        stale_mappings = AccountMapping.objects.exclude(
            account_user_id__in=api_account_numbers
        ).filter(is_manual=False).exclude(access_ids__is_manual=True).distinct()

        # Get related AccessId records
        stale_mapping_ids = list(stale_mappings.values_list('id', flat=True))
        stale_access_ids = AccessId.objects.filter(
            mapping_id__in=stale_mapping_ids
        )

        self.stdout.write(self.style.HTTP_INFO("=" * 80))
        self.stdout.write(f"Mode: {'DRY RUN (no deletions will be made)' if dry_run else 'LIVE (deletions will be performed)'}")
        self.stdout.write(self.style.HTTP_INFO("=" * 80 + "\n"))

        # Display what will be deleted
        if stale_mappings.exists():
            self.stdout.write(
                self.style.WARNING(f"\n🗑️  AccountMapping records to delete: {stale_mappings.count()}\n")
            )
            self.stdout.write(self.style.HTTP_INFO("-" * 80))

            for mapping in stale_mappings[:10]:  # Show first 10
                self.stdout.write(f"  ID: {mapping.id}")
                self.stdout.write(f"  User ID: {mapping.account_user_id}")
                self.stdout.write(f"  Name: {mapping.get_full_name()}")
                self.stdout.write(f"  System: {mapping.accounting_system}")
                self.stdout.write("")

            if stale_mappings.count() > 10:
                self.stdout.write(f"  ... and {stale_mappings.count() - 10} more records")
            self.stdout.write(self.style.HTTP_INFO("-" * 80))
        else:
            self.stdout.write(self.style.SUCCESS("✓ No stale AccountMapping records found"))

        if stale_access_ids.exists():
            self.stdout.write(
                self.style.WARNING(f"\n🗑️  AccessId records to delete: {stale_access_ids.count()}\n")
            )
            self.stdout.write(self.style.HTTP_INFO("-" * 80))

            for access_id in stale_access_ids[:10]:  # Show first 10
                self.stdout.write(f"  ID: {access_id.id}")
                self.stdout.write(f"  Access ID: {access_id.access_id}")
                self.stdout.write(f"  Auth Type: {access_id.auth_type}")
                self.stdout.write(f"  Mapping: {access_id.mapping.account_user_id if access_id.mapping else 'None'}")
                self.stdout.write("")

            if stale_access_ids.count() > 10:
                self.stdout.write(f"  ... and {stale_access_ids.count() - 10} more records")
            self.stdout.write(self.style.HTTP_INFO("-" * 80))
        else:
            self.stdout.write(self.style.SUCCESS("✓ No stale AccessId records found"))

        # Confirmation prompt (unless forced)
        if not dry_run and not force and (stale_mappings.exists() or stale_access_ids.exists()):
            self.stdout.write("")
            confirm = input(
                f"Are you sure you want to delete {stale_mappings.count()} AccountMapping "
                f"and {stale_access_ids.count()} AccessId records? (yes/no): "
            )
            if confirm.lower() not in ['yes', 'y']:
                self.stdout.write(self.style.WARNING("Operation cancelled by user"))
                return

        # Perform deletions
        if stale_mappings.exists() or stale_access_ids.exists():
            if dry_run:
                self.stdout.write(
                    self.style.HTTP_INFO(
                        f"\n✓ DRY RUN: Would delete {stale_mappings.count()} AccountMapping "
                        f"and {stale_access_ids.count()} AccessId records"
                    )
                )
                self.stdout.write(
                    self.style.HTTP_INFO(
                        "Run without --dry-run flag to perform deletions\n"
                    )
                )
                self._export_dry_run_to_csv(stale_mappings, stale_access_ids, export_csv)
            else:
                self.stdout.write("\n🔄 Performing deletions...\n")

                # Delete AccessId records first (due to foreign key constraint)
                access_deleted = 0
                for access_id in stale_access_ids:
                    try:
                        access_id.delete()
                        access_deleted += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✓ Deleted AccessId {access_id.id}: {access_id.access_id}"
                            )
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"✗ Failed to delete AccessId {access_id.id}: {str(e)}"
                            )
                        )

                # Delete AccountMapping records
                mapping_deleted = 0
                for mapping in stale_mappings:
                    try:
                        mapping.delete()
                        mapping_deleted += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✓ Deleted AccountMapping {mapping.id}: {mapping.account_user_id}"
                            )
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"✗ Failed to delete AccountMapping {mapping.id}: {str(e)}"
                            )
                        )

                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n✓ Successfully deleted {mapping_deleted} AccountMapping "
                        f"and {access_deleted} AccessId records\n"
                    )
                )
        else:
            if not dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        "\n✓ No deletions needed (all records are present in API)\n"
                    )
                )

        # Summary
        self.stdout.write(self.style.HTTP_INFO("=" * 80))
        self.stdout.write(f"API accountNumber values: {len(api_account_numbers)}")
        self.stdout.write(f"AccountMapping records to delete: {stale_mappings.count()}")
        self.stdout.write(f"AccessId records to delete: {stale_access_ids.count()}")
        self.stdout.write(self.style.HTTP_INFO("=" * 80 + "\n"))

    def _export_dry_run_to_csv(self, stale_mappings, stale_access_ids, csv_file_path):
        rows = []

        for mapping in stale_mappings:
            rows.append({
                "row_type": "AccountMapping",
                "account_mapping_id": mapping.id,
                "account_user_id": mapping.account_user_id,
                "first_name": mapping.first_name or "",
                "last_name": mapping.last_name or "",
                "narration": mapping.narration or "",
                "accounting_system": mapping.accounting_system,
                "accounting_group": mapping.accounting_group or "",
                "usd_balance": str(mapping.usd_balance) if mapping.usd_balance is not None else "",
                "zwg_balance": str(mapping.zwg_balance) if mapping.zwg_balance is not None else "",
                "access_id_id": "",
                "access_id": "",
                "auth_type": "",
                "mapping_id": "",
            })

        for access_id in stale_access_ids:
            rows.append({
                "row_type": "AccessId",
                "account_mapping_id": access_id.mapping_id or "",
                "account_user_id": access_id.mapping.account_user_id if access_id.mapping else "",
                "first_name": "",
                "last_name": "",
                "narration": "",
                "accounting_system": "",
                "accounting_group": "",
                "usd_balance": "",
                "zwg_balance": "",
                "access_id_id": access_id.id,
                "access_id": access_id.access_id or "",
                "auth_type": access_id.auth_type or "",
                "mapping_id": access_id.mapping_id or "",
            })

        fieldnames = [
            "row_type",
            "account_mapping_id",
            "account_user_id",
            "first_name",
            "last_name",
            "narration",
            "accounting_system",
            "accounting_group",
            "usd_balance",
            "zwg_balance",
            "access_id_id",
            "access_id",
            "auth_type",
            "mapping_id",
        ]

        try:
            with open(csv_file_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            self.stdout.write(self.style.SUCCESS(f"✓ Exported dry-run deletion candidates to {csv_file_path}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ Failed to export dry-run CSV: {str(e)}"))