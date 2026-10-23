import csv
from django.core.management.base import BaseCommand, CommandError
import requests


class Command(BaseCommand):
    help = "Fetch AllMemberBalances, find duplicate cardId values, and export matching rows to CSV"

    def add_arguments(self, parser):
        parser.add_argument(
            "--api-url",
            type=str,
            default="https://squashgate.paradigmuser.com/api/AllMemberBalances",
            help="API endpoint URL to fetch AllMemberBalances from",
        )
        parser.add_argument(
            "--output-file",
            type=str,
            default="duplicate_cardids.csv",
            help="CSV file path to write duplicate rows to",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=30,
            help="HTTP request timeout in seconds",
        )

    def handle(self, *args, **options):
        api_url = options["api_url"]
        output_file = options["output_file"]
        timeout = options["timeout"]

        self.stdout.write(self.style.HTTP_INFO(f"\n📡 Fetching API data from: {api_url}\n"))

        try:
            response = requests.get(api_url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise CommandError(f"API request timed out after {timeout} seconds")
        except requests.exceptions.ConnectionError:
            raise CommandError(f"Failed to connect to API: {api_url}")
        except requests.exceptions.RequestException as exc:
            raise CommandError(f"API request failed: {exc}")

        try:
            api_data = response.json()
        except ValueError:
            raise CommandError("API response is not valid JSON")

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

        self.stdout.write(f"✓ Loaded {len(items)} items from API\n")

        # Normalize items and compute duplicate cardIds
        duplicates = {}
        cardid_counts = {}
        normalized_items = []

        for item in items:
            if not isinstance(item, dict):
                continue

            card_id = item.get("cardId")
            if card_id is None:
                continue

            card_id = str(card_id).strip()
            if card_id == "":
                continue

            normalized_items.append(item)
            cardid_counts[card_id] = cardid_counts.get(card_id, 0) + 1

        duplicate_cardids = {card for card, count in cardid_counts.items() if count > 1}

        if not duplicate_cardids:
            self.stdout.write(self.style.SUCCESS("No duplicate cardId values found."))
            return

        self.stdout.write(self.style.WARNING(f"Found {len(duplicate_cardids)} duplicate cardId(s)\n"))

        for card_id in sorted(duplicate_cardids):
            self.stdout.write(f"  - {card_id} ({cardid_counts[card_id]} rows)")

        rows_to_export = [item for item in normalized_items if str(item.get("cardId")).strip() in duplicate_cardids]
        rows_to_export.sort(key=lambda item: str(item.get("cardId", "")).strip())

        if not rows_to_export:
            raise CommandError("Duplicate cardId values were detected but no rows could be exported.")

        fieldnames = set()
        for row in rows_to_export:
            fieldnames.update(row.keys())

        fieldnames = sorted(fieldnames)

        try:
            with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for row in rows_to_export:
                    writer.writerow({key: row.get(key, "") for key in fieldnames})
        except Exception as exc:
            raise CommandError(f"Failed to write CSV file: {exc}")

        self.stdout.write(self.style.SUCCESS(f"\n✓ Exported {len(rows_to_export)} rows to {output_file}"))
        self.stdout.write(self.style.SUCCESS(f"Duplicate cardId count: {len(duplicate_cardids)}"))
