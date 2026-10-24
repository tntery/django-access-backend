import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from access.models import AccessId, AccountMapping


class Command(BaseCommand):
    help = (
        "Upload additional access IDs from a CSV file with columns: "
        "account_user_id, access_id_1, access_id_2."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_file",
            type=str,
            help="Path to the CSV file to import.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without saving changes.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_file"])
        dry_run = options["dry_run"]

        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                if reader.fieldnames is None:
                    raise CommandError("CSV file does not contain a header row")

                normalized_headers = {}
                for header in reader.fieldnames:
                    if not header:
                        continue
                    clean_header = header.strip().lstrip('\ufeff').lower()
                    if clean_header:
                        normalized_headers[clean_header] = header

                required_header = "account_user_id"
                if required_header not in normalized_headers:
                    raise CommandError(
                        "CSV header must include 'account_user_id'"
                    )

                access_headers = [
                    header
                    for key, header in normalized_headers.items()
                    if key in {"access_id_1", "access_id_2"}
                ]
                if not access_headers:
                    raise CommandError(
                        "CSV header must include at least one of 'access_id_1' or 'access_id_2'"
                    )

                summary = {
                    "rows": 0,
                    "processed": 0,
                    "skipped_empty_account_id": 0,
                    "skipped_missing_mapping": 0,
                    "skipped_no_access_ids": 0,
                    "access_ids_written": 0,
                    "access_ids_skipped": 0,
                }
                actions = []

                for row_number, raw_row in enumerate(reader, start=2):
                    summary["rows"] += 1

                    # Skip rows that are entirely blank
                    if not any((value or "").strip() for value in raw_row.values()):
                        continue

                    account_user_id = self._clean_value(raw_row.get(normalized_headers[required_header]))
                    if not account_user_id:
                        summary["skipped_empty_account_id"] += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"Row {row_number}: skipped because account_user_id is empty"
                            )
                        )
                        continue

                    mapping = AccountMapping.objects.filter(account_user_id=account_user_id).first()
                    if not mapping:
                        summary["skipped_missing_mapping"] += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"Row {row_number}: no AccountMapping found for account_user_id={account_user_id}"
                            )
                        )
                        continue

                    row_access_ids = []
                    for access_col in ["access_id_1", "access_id_2"]:
                        normalized_key = access_col
                        header_name = normalized_headers.get(normalized_key)
                        if not header_name:
                            continue
                        access_id_value = self._clean_value(raw_row.get(header_name))
                        if access_id_value:
                            row_access_ids.append(access_id_value)

                    if not row_access_ids:
                        summary["skipped_no_access_ids"] += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"Row {row_number}: skipped because no access IDs were present"
                            )
                        )
                        continue

                    summary["processed"] += 1

                    for access_id in row_access_ids:
                        if dry_run:
                            actions.append((row_number, account_user_id, access_id, mapping.id))
                        else:
                            access_obj, created = AccessId.objects.update_or_create(
                                access_id=access_id,
                                defaults={"mapping": mapping},
                            )
                            summary["access_ids_written"] += 1
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"Row {row_number}: {'created' if created else 'updated'} AccessId={access_id} -> mapping={account_user_id}"
                                )
                            )

                if dry_run:
                    if actions:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Dry run: {len(actions)} access IDs would be created/updated"
                            )
                        )
                        for row_number, account_user_id, access_id, mapping_id in actions:
                            self.stdout.write(
                                f"Row {row_number}: AccessId={access_id} -> AccountMapping(id={mapping_id}, account_user_id={account_user_id})"
                            )
                    else:
                        self.stdout.write(self.style.WARNING("Dry run: no access IDs would be imported."))
                else:
                    if summary["access_ids_written"]:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Successfully imported {summary['access_ids_written']} access IDs"
                            )
                        )
                    else:
                        self.stdout.write(self.style.WARNING("No access IDs were imported."))

                self.stdout.write("\nSummary:")
                self.stdout.write(f"  Rows read: {summary['rows']}")
                self.stdout.write(f"  Rows processed: {summary['processed']}")
                self.stdout.write(f"  Empty account_user_id skipped: {summary['skipped_empty_account_id']}")
                self.stdout.write(f"  Missing mapping skipped: {summary['skipped_missing_mapping']}")
                self.stdout.write(f"  Rows without access IDs skipped: {summary['skipped_no_access_ids']}")
                if not dry_run:
                    self.stdout.write(f"  Access IDs written: {summary['access_ids_written']}")

        except csv.Error as exc:
            raise CommandError(f"Invalid CSV file: {exc}")

    def _clean_value(self, value):
        if value is None:
            return ""
        return str(value).strip()
