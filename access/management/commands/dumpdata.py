import csv
from datetime import datetime
from django.core.management.base import BaseCommand
from django.apps import apps
from django.db.models import ForeignKey, ManyToOneRel
from access.models import (
    AccountMapping,
    AccessId,
    AccessControlDevice,
    PendingAccountMapping,
    AccessEventLog,
    AccessDeviceSyncLog,
    TamperEventLog,
    MappingModalState,
    SystemMetadata,
    Setting,
)


class Command(BaseCommand):
    help = "Dump all database tables to CSV files with expanded foreign keys"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            type=str,
            default=".",
            help="Directory to save CSV files (default: current directory)",
        )
        parser.add_argument(
            "--timestamp",
            action="store_true",
            help="Add timestamp to filenames",
        )

    def handle(self, *args, **options):
        output_dir = options["output_dir"]
        add_timestamp = options["timestamp"]
        
        # Get all models from the access app
        models_to_dump = [
            AccountMapping,
            AccessId,
            AccessControlDevice,
            PendingAccountMapping,
            AccessEventLog,
            AccessDeviceSyncLog,
            TamperEventLog,
            MappingModalState,
            SystemMetadata,
            Setting,
        ]

        timestamp_suffix = f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}" if add_timestamp else ""

        for model in models_to_dump:
            self.dump_model_to_csv(model, output_dir, timestamp_suffix)

        self.stdout.write(
            self.style.SUCCESS("✓ All data exported successfully to CSV files")
        )

    def dump_model_to_csv(self, model, output_dir, timestamp_suffix):
        """Dump a single model to CSV with expanded foreign keys"""
        model_name = model.__name__
        filename = f"{output_dir}/{model_name}{timestamp_suffix}.csv"

        queryset = model.objects.all()

        if not queryset.exists():
            self.stdout.write(f"  ⊘ {model_name}: No data (skipped)")
            return

        # Get all fields including foreign keys
        fields = self._get_fields_with_fk_expansion(model)
        field_names = [f["name"] for f in fields]

        try:
            with open(filename, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=field_names)
                writer.writeheader()

                for obj in queryset:
                    row = self._get_object_row(obj, fields)
                    writer.writerow(row)

            count = queryset.count()
            self.stdout.write(f"  ✓ {model_name}: {count} records exported")
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"  ✗ {model_name}: Error - {str(e)}")
            )

    def _get_fields_with_fk_expansion(self, model):
        """Get model fields with strategy for expanding foreign keys"""
        fields = []
        meta = model._meta

        for field in meta.get_fields():
            # Skip reverse relations (OneToOneRel, ManyToOneRel, etc.)
            if isinstance(field, ManyToOneRel):
                continue

            if isinstance(field, ForeignKey):
                # For foreign keys, add multiple columns to show related data
                related_model = field.related_model
                
                # Add the ID field
                fields.append({
                    "name": f"{field.name}_id",
                    "field": field,
                    "type": "fk_id",
                })
                
                # Add a display field with the related object's string representation
                fields.append({
                    "name": f"{field.name}_display",
                    "field": field,
                    "type": "fk_display",
                })
                
                # Add key fields from the related model
                related_meta = related_model._meta
                for related_field in related_meta.get_fields():
                    if related_field.name in ["id"]:
                        continue
                    if isinstance(related_field, ManyToOneRel):
                        continue
                    if isinstance(related_field, ForeignKey):
                        continue
                    
                    # Add the first few key fields from the related model
                    related_pk_name = related_meta.pk.name
                    if related_field.name == related_pk_name:
                        continue
                    
                    # Add a few important fields
                    if related_field.name in ["account_user_id", "name", "title", "device_id", "access_id"]:
                        fields.append({
                            "name": f"{field.name}_{related_field.name}",
                            "field": field,
                            "type": "fk_related_field",
                            "related_field_name": related_field.name,
                        })
            else:
                # Regular field
                fields.append({
                    "name": field.name,
                    "field": field,
                    "type": "regular",
                })

        return fields

    def _get_object_row(self, obj, fields):
        """Extract data from an object for a CSV row"""
        row = {}

        for field_spec in fields:
            field_name = field_spec["name"]
            field_type = field_spec["type"]

            try:
                if field_type == "regular":
                    # Regular field: just get the value
                    value = getattr(obj, field_spec["field"].name)
                    # Convert complex types to strings
                    if value is None:
                        row[field_name] = ""
                    elif isinstance(value, (list, dict)):
                        row[field_name] = str(value)
                    else:
                        row[field_name] = value

                elif field_type == "fk_id":
                    # Foreign key ID
                    related_obj = getattr(obj, field_spec["field"].name)
                    row[field_name] = related_obj.id if related_obj else ""

                elif field_type == "fk_display":
                    # Foreign key display (string representation)
                    related_obj = getattr(obj, field_spec["field"].name)
                    row[field_name] = str(related_obj) if related_obj else ""

                elif field_type == "fk_related_field":
                    # Field from the related object
                    related_obj = getattr(obj, field_spec["field"].name)
                    if related_obj:
                        related_value = getattr(
                            related_obj,
                            field_spec["related_field_name"],
                            ""
                        )
                        row[field_name] = related_value
                    else:
                        row[field_name] = ""

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"    Warning: Could not extract {field_name} from {obj}: {str(e)}"
                    )
                )
                row[field_name] = ""

        return row
