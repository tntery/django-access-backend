import csv
import requests
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from access.models import AccountMapping


class Command(BaseCommand):
    help = "Update AccountMapping.account_user_id from API data (AllMemberBalances)"

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
            help="Preview changes without modifying database",
        )
        parser.add_argument(
            "--export-csv",
            type=str,
            help="Export dry-run results to CSV file (only works with --dry-run)",
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
        export_csv = options.get("export_csv")
        timeout = options["timeout"]

        # Validate arguments
        if export_csv and not dry_run:
            raise CommandError("--export-csv can only be used with --dry-run")

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
            # Check if there's a data/results/items key
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
            self.stdout.write(self.style.WARNING("⚠ No data received from API"))
            return

        self.stdout.write(f"✓ Received {len(items)} items from API\n")
        self.stdout.write(self.style.HTTP_INFO("=" * 80))
        self.stdout.write(f"Mode: {'DRY RUN (no changes will be made)' if dry_run else 'LIVE (changes will be saved)'}")
        self.stdout.write(self.style.HTTP_INFO("=" * 80 + "\n"))

        # Process updates
        updates_to_perform = []
        skipped = []

        for idx, item in enumerate(items, 1):
            # Validate required fields
            if "cardId" not in item:
                skipped.append({
                    "reason": "Missing 'cardId' field",
                    "item": item,
                })
                continue

            if "accountNumber" not in item:
                skipped.append({
                    "reason": "Missing 'accountNumber' field",
                    "item": item,
                })
                continue

            card_id = str(item["cardId"]).strip()
            account_number = str(item["accountNumber"]).strip()

            if not card_id or not account_number:
                skipped.append({
                    "reason": "Empty cardId or accountNumber",
                    "item": item,
                })
                continue

            # Find matching AccountMapping records
            matching_mappings = AccountMapping.objects.filter(
                account_user_id=card_id, is_manual=False,
            )

            if matching_mappings.exists():
                for mapping in matching_mappings:
                    updates_to_perform.append({
                        "mapping": mapping,
                        "old_id": mapping.account_user_id,
                        "new_id": account_number,
                        "api_item": item,
                    })
            else:
                skipped.append({
                    "reason": f"No AccountMapping found for cardId='{card_id}'",
                    "item": item,
                })

        # Display updates that will be performed
        if updates_to_perform:
            self.stdout.write(
                self.style.SUCCESS(f"\n📝 Updates to perform: {len(updates_to_perform)}\n")
            )
            self.stdout.write(self.style.HTTP_INFO("-" * 80))

            for idx, update in enumerate(updates_to_perform, 1):
                mapping = update["mapping"]
                old_id = update["old_id"]
                new_id = update["new_id"]
                
                self.stdout.write(f"\n{idx}. Update AccountMapping (ID: {mapping.id})")
                self.stdout.write(f"   Name: {mapping.get_full_name()}")
                self.stdout.write(f"   Old account_user_id: {old_id}")
                self.stdout.write(f"   New account_user_id: {new_id}")
                self.stdout.write(f"   System: {mapping.accounting_system}")

            self.stdout.write(self.style.HTTP_INFO("-" * 80))
        else:
            self.stdout.write(self.style.WARNING("⚠ No matching records found for updates"))

        # Display skipped items
        if skipped:
            self.stdout.write(f"\n⊘ Skipped: {len(skipped)} items\n")
            self.stdout.write(self.style.HTTP_INFO("-" * 80))

            for idx, skip in enumerate(skipped, 1):
                self.stdout.write(
                    self.style.WARNING(
                        f"{idx}. {skip['reason']}\n   Item: {skip['item']}"
                    )
                )

            self.stdout.write(self.style.HTTP_INFO("-" * 80))

        # Perform updates if not dry-run
        if updates_to_perform:
            if dry_run:
                self.stdout.write(
                    self.style.HTTP_INFO(
                        f"\n✓ DRY RUN: Would update {len(updates_to_perform)} records"
                    )
                )
                self.stdout.write(
                    self.style.HTTP_INFO(
                        "Run without --dry-run flag to apply changes\n"
                    )
                )

                # Export to CSV if requested
                if options.get("export_csv"):
                    self._export_dry_run_to_csv(updates_to_perform, options["export_csv"])
            else:
                self.stdout.write("\n🔄 Applying updates...\n")

                for update in updates_to_perform:
                    mapping = update["mapping"]
                    new_id = update["new_id"]

                    try:
                        mapping.account_user_id = new_id
                        mapping.save()
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✓ Updated AccountMapping {mapping.id}: "
                                f"{update['old_id']} → {new_id}"
                            )
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(
                                f"✗ Failed to update AccountMapping {mapping.id}: {str(e)}"
                            )
                        )

                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n✓ Successfully updated {len(updates_to_perform)} record(s)\n"
                    )
                )
        else:
            if not dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        "\n⚠ No updates performed (no matching records)\n"
                    )
                )

        # Summary
        self.stdout.write(self.style.HTTP_INFO("=" * 80))
        self.stdout.write(f"Total items from API: {len(items)}")
        self.stdout.write(f"Updates prepared: {len(updates_to_perform)}")
        self.stdout.write(f"Skipped: {len(skipped)}")
        self.stdout.write(self.style.HTTP_INFO("=" * 80 + "\n"))

    def _export_dry_run_to_csv(self, updates_to_perform, csv_file_path):
        """Export dry-run results to CSV file"""
        if not updates_to_perform:
            self.stdout.write(self.style.WARNING("No updates to export"))
            return

        fieldnames = [
            "account_mapping_id",
            "current_account_user_id",
            "new_account_user_id",
            "first_name",
            "last_name",
            "narration",
            "accounting_system",
            "accounting_group",
            "usd_balance",
            "zwg_balance",
            "api_card_id",
            "api_account_number",
            "export_timestamp",
        ]

        try:
            with open(csv_file_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()

                for update in updates_to_perform:
                    mapping = update["mapping"]
                    api_item = update["api_item"]

                    row = {
                        "account_mapping_id": mapping.id,
                        "current_account_user_id": update["old_id"],
                        "new_account_user_id": update["new_id"],
                        "first_name": mapping.first_name or "",
                        "last_name": mapping.last_name or "",
                        "narration": mapping.narration or "",
                        "accounting_system": mapping.accounting_system,
                        "accounting_group": mapping.accounting_group or "",
                        "usd_balance": str(mapping.usd_balance) if mapping.usd_balance else "",
                        "zwg_balance": str(mapping.zwg_balance) if mapping.zwg_balance else "",
                        "api_card_id": api_item.get("cardId", ""),
                        "api_account_number": api_item.get("accountNumber", ""),
                        "export_timestamp": datetime.now().isoformat(),
                    }
                    writer.writerow(row)

            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Exported {len(updates_to_perform)} dry-run updates to {csv_file_path}"
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"✗ Failed to export CSV: {str(e)}")
            )
