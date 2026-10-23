import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from access.models import AccessControlDevice, AccessId, AccountMapping


class Command(BaseCommand):
    help = (
        "Assign AccessId records to AccessControlDevice 151BSC26001 and/or 151BSC26003 "
        "using two supported CSV formats."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--device-151bsc26001-csv",
            type=str,
            help=(
                "CSV file for AccessControlDevice 151BSC26001. "
                "Expected headers: mapping_account_user_id, access_id, auth_type."
            ),
        )
        parser.add_argument(
            "--device-151bsc26003-csv",
            type=str,
            help=(
                "CSV file for AccessControlDevice 151BSC26003. "
                "Expected headers: account_user_id, access_id_1, access_id_2."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without saving anything.",
        )

    def handle(self, *args, **options):
        device_151bsc26001_csv = options.get("device_151bsc26001_csv")
        device_151bsc26003_csv = options.get("device_151bsc26003_csv")
        dry_run = options["dry_run"]

        if not device_151bsc26001_csv and not device_151bsc26003_csv:
            raise CommandError(
                "Provide at least one CSV with --device-151bsc26001-csv or --device-151bsc26003-csv"
            )

        device_151bsc26001 = self._get_or_create_device("151BSC26001")
        device_151bsc26003 = self._get_or_create_device("151BSC26003")

        if device_151bsc26001_csv:
            self._process_device_151bsc26001(device_151bsc26001_csv, device_151bsc26001, dry_run)

        if device_151bsc26003_csv:
            self._process_device_151bsc26003(device_151bsc26003_csv, device_151bsc26003, dry_run)

    def _process_device_151bsc26001(self, csv_path: str, device: AccessControlDevice, dry_run: bool):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"CSV file not found: {path}")

        required_columns = [
            "mapping_account_user_id",
            "access_id",
            "auth_type",
        ]
        summary = {
            "rows": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "missing_mapping": 0,
        }

        with path.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            if reader.fieldnames is None:
                raise CommandError("CSV file does not contain a header row")

            headers = self._normalize_headers(reader.fieldnames)
            self._validate_headers(headers, required_columns)

            self.stdout.write(self.style.HTTP_INFO("\nProcessing 151BSC26001 CSV"))
            self.stdout.write(self.style.HTTP_INFO("=" * 60))

            for row_number, row in enumerate(reader, start=2):
                if not any(self._clean_value(row.get(headers[col])) for col in required_columns):
                    continue

                summary["rows"] += 1
                account_user_id = self._clean_value(row.get(headers["mapping_account_user_id"]))
                access_id = self._clean_value(row.get(headers["access_id"]))
                auth_type = self._clean_value(row.get(headers["auth_type"]))

                if not access_id:
                    summary["skipped"] += 1
                    self.stdout.write(self.style.WARNING(
                        f"Row {row_number}: skipped because access_id is empty"
                    ))
                    continue

                if not auth_type:
                    summary["skipped"] += 1
                    self.stdout.write(self.style.WARNING(
                        f"Row {row_number}: skipped because auth_type is empty for access_id={access_id}"
                    ))
                    continue

                mapping = None
                if account_user_id:
                    mapping = AccountMapping.objects.filter(account_user_id=account_user_id).first()
                    if not mapping:
                        summary["missing_mapping"] += 1

                if dry_run:
                    summary["created"] += 1
                    self.stdout.write(
                        f"Row {row_number}: would create/update AccessId={access_id} "
                        f"auth_type={auth_type} device={device.device_id} mapping={account_user_id or 'None'}"
                    )
                else:
                    action = self._update_or_create_access_id(
                        access_id=access_id,
                        device=device,
                        auth_type=auth_type,
                        mapping=mapping,
                    )
                    summary[action] += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Row {row_number}: {action} AccessId={access_id} "
                            f"auth_type={auth_type} device={device.device_id} mapping={account_user_id or 'None'}"
                        )
                    )

        self.stdout.write(self.style.SUCCESS(
            f"151BSC26001: processed {summary['rows']} rows, created/updated {summary['created'] + summary['updated']} AccessId(s), skipped {summary['skipped']} rows, missing mapping {summary['missing_mapping']}"
        ))

    def _process_device_151bsc26003(self, csv_path: str, device: AccessControlDevice, dry_run: bool):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"CSV file not found: {path}")

        required_columns = [
            "account_user_id",
            "access_id_1",
            "access_id_2",
        ]
        summary = {
            "rows": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "missing_mapping": 0,
        }

        with path.open("r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            if reader.fieldnames is None:
                raise CommandError("CSV file does not contain a header row")

            headers = self._normalize_headers(reader.fieldnames)
            self._validate_headers(headers, required_columns)

            self.stdout.write(self.style.HTTP_INFO("\nProcessing 151BSC26003 CSV"))
            self.stdout.write(self.style.HTTP_INFO("=" * 60))

            for row_number, row in enumerate(reader, start=2):
                account_user_id = self._clean_value(row.get(headers["account_user_id"]))
                if not account_user_id:
                    summary["skipped"] += 1
                    self.stdout.write(self.style.WARNING(
                        f"Row {row_number}: skipped because account_user_id is empty"
                    ))
                    continue

                summary["rows"] += 1
                mapping = AccountMapping.objects.filter(account_user_id=account_user_id).first()
                if not mapping:
                    summary["missing_mapping"] += 1

                for column_name in ["access_id_1", "access_id_2"]:
                    access_id = self._clean_value(row.get(headers[column_name]))
                    if not access_id:
                        continue

                    auth_type = "biometric"
                    if dry_run:
                        summary["created"] += 1
                        self.stdout.write(
                            f"Row {row_number}: would create/update AccessId={access_id} "
                            f"auth_type={auth_type} device={device.device_id} mapping={account_user_id}"
                        )
                    else:
                        action = self._update_or_create_access_id(
                            access_id=access_id,
                            device=device,
                            auth_type=auth_type,
                            mapping=mapping,
                        )
                        summary[action] += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Row {row_number}: {action} AccessId={access_id} "
                                f"auth_type={auth_type} device={device.device_id} mapping={account_user_id}"
                            )
                        )

        self.stdout.write(self.style.SUCCESS(
            f"151BSC26003: processed {summary['rows']} rows, created/updated {summary['created'] + summary['updated']} AccessId(s), skipped {summary['skipped']} rows, missing mapping {summary['missing_mapping']}"
        ))

    def _update_or_create_access_id(
        self,
        access_id: str,
        device: AccessControlDevice,
        auth_type: str,
        mapping: AccountMapping | None,
    ) -> str:
        qs = AccessId.objects.filter(access_id=access_id)
        if qs.exists():
            qs.update(
                access_device=device,
                auth_type=auth_type,
                mapping=mapping,
            )
            return "updated"

        AccessId.objects.create(
            access_id=access_id,
            access_device=device,
            auth_type=auth_type,
            mapping=mapping,
        )
        return "created"

    def _normalize_headers(self, fieldnames):
        return {
            header.strip().lstrip("\ufeff").lower(): header
            for header in fieldnames
            if header and header.strip()
        }

    def _validate_headers(self, headers, required_columns):
        missing = [col for col in required_columns if col not in headers]
        if missing:
            raise CommandError(
                f"Missing required CSV headers: {', '.join(missing)}"
            )

    def _get_or_create_device(self, device_id: str) -> AccessControlDevice:
        device, created = AccessControlDevice.objects.get_or_create(
            device_id=device_id,
            defaults={"alias": device_id},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created AccessControlDevice {device_id}"))
        return device

    def _clean_value(self, value):
        if value is None:
            return ""
        return str(value).strip()
