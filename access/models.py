from django.db import models
from solo.models import SingletonModel

class AccountMapping(models.Model):
    """Model to store the mapping between access control device IDs and accounting system IDs"""

    ACCOUNTING_SYSTEM_CHOICES = (
        ("palladium", "Palladium"),
        ("quickbooks", "QuickBooks"),
        ("xero", "Xero"),
    )
    ACCOUNTING_GROUP_CHOICES = (
        ("main_bar", "Main Bar"),
        ("squash", "Squash Court"),
    )
    ACCESS_OVERRIDE_CHOICES = (
        ("auto", "Auto"),
        ("grant", "Grant"),
        ("reject", "Reject"),
    )
    account_user_id = models.CharField(max_length=100, unique=True, help_text="Unique identifier from accounting system")
    first_name = models.CharField(max_length=100, null=True, blank=True, help_text="User's first name")
    last_name = models.CharField(max_length=100, null=True, blank=True, help_text="User's last name")
    narration = models.CharField(max_length=100, null=True, blank=True, help_text="User's description/narration from accounting system")
    usd_balance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="User's balance in USD")
    zwg_balance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="User's balance in ZWG")
    accounting_system = models.CharField(max_length=50, choices=ACCOUNTING_SYSTEM_CHOICES, default="palladium", help_text="Accounting system name")
    accounting_group = models.CharField(max_length=100, null=True, blank=True, choices=ACCOUNTING_GROUP_CHOICES, help_text="Accounting group or category for the user")
    access_override = models.CharField(max_length=6, choices=ACCESS_OVERRIDE_CHOICES, default="auto", help_text="Local access decision; external user syncs do not change this value.")
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['narration', '-last_name', '-first_name']  # Order by last name, then first name for easier lookup in the UI

    def get_full_name(self):
        """Helper method to return full name of the user"""
        if self.narration:
            return self.narration
        elif self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        elif self.last_name:
            return self.last_name
        else:
            return "Unknown User"

    def __str__(self):
        return f"Account-{self.account_user_id} | {self.get_full_name()} | System-{self.accounting_system}"

    @property
    def access_decision_type(self):
        return "Auto" if self.access_override == "auto" else "Manual"

    @property
    def manual_access_decision(self):
        return self.access_override.upper() if self.access_override in ("grant", "reject") else None

    @property
    def device_access_id(self):
        """Backwards-compatible accessor for the first linked access ID."""
        first_access = self.access_ids.order_by('created_at').first()
        return first_access.access_id if first_access else None

    @property
    def has_access_id(self):
        """True when at least one access ID is linked to this account."""
        return self.access_ids.filter(access_id__isnull=False).exclude(access_id='').exists()

class AccessId(models.Model):
    AUTH_TYPES = (
        ("card", "Card"),
        ("biometric", "Biometric"),
        ("pin", "PIN"),
        ("other", "Other"),
    )
    access_id = models.CharField(max_length=100, null=True, blank=True, help_text="Unique identifier from access control device")
    access_device = models.ForeignKey('AccessControlDevice', related_name='access_ids', on_delete=models.SET_NULL, null=True, blank=True, help_text="The access control device this ID is associated with")
    auth_type = models.CharField(max_length=50, null=True, blank=True, choices=AUTH_TYPES, help_text="Type of authentication (e.g., card, biometric)")
    mapping = models.ForeignKey(AccountMapping, on_delete=models.SET_NULL, null=True, blank=True, help_text="Linked account mapping if available", related_name="access_ids")
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("access_id", "access_device")

    def __str__(self):
        mapped_user = self.mapping.account_user_id if self.mapping else None
        return f"AccessId: {self.access_id} | mapped_user={mapped_user} | auth_type={self.auth_type}"

class AccessControlDevice(models.Model):
    """Model to represent access control devices and their status"""
    device_id = models.CharField(max_length=100, unique=True, help_text="Unique identifier for the access control device")
    alias = models.CharField(max_length=100, null=True, blank=True, help_text="Human-readable alias for the device")
    location = models.CharField(max_length=100, null=True, blank=True, help_text="Physical location of the device")
    is_active = models.BooleanField(default=True, help_text="Whether the device is active and should be processed")
    last_sync_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last successful sync with this device")

    def __str__(self):
        return f"AccessControlDevice: {self.device_id} | Alias: {self.alias} | Location: {self.location} | Active: {self.is_active}"

class PendingAccountMapping(models.Model):
    """Model to temporarily store access attempts before mapping is confirmed"""
    device_access_id = models.CharField(max_length=50, unique=True, null=True, blank=True, help_text="Unique identifier from access control device")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Pending Account Mapping: {self.device_access_id} at {self.created_at}"

class AccessEventLog(models.Model):
    """Model to log access attempts and their mapping status"""
    ACCESS_STATUSES = (
        ("grant", "Grant"),
        ("reject", "Reject"),
    )
    ACCESS_TYPES = (
        ("normal", "Normal Access Attempt"),
        ("one_step_mapping", "One-Step Mapping Attempt"),
    )
    device_access_id = models.CharField(max_length=50, help_text="Unique identifier from access control device")
    user = models.ForeignKey(AccountMapping, on_delete=models.SET_NULL, null=True, blank=True, help_text="Linked account mapping if available")
    access_status = models.CharField(max_length=10, choices=ACCESS_STATUSES)
    decision_type = models.CharField(max_length=6, choices=(("auto", "Auto"), ("manual", "Manual")), default="auto")
    access_type = models.CharField(max_length=20, choices=ACCESS_TYPES, default="normal")
    access_control_device = models.ForeignKey(AccessControlDevice, on_delete=models.SET_NULL, null=True, blank=True, help_text="The access control device where this event occurred")
    event_timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"AccessEventLog: {self.device_access_id} | {self.user.account_user_id if self.user else None} | {self.user.accounting_system if self.user else None} | {self.access_status} at {self.event_timestamp}"
    
class AccessDeviceSyncLog(models.Model):
    """Model to log sync attempts with access control devices"""
    device = models.ForeignKey(AccessControlDevice, on_delete=models.SET_NULL, null=True, blank=True, help_text="The access control device being synced")
    success = models.BooleanField(default=False, help_text="Whether the sync attempt was successful")
    error_message = models.TextField(null=True, blank=True, help_text="Error message if the sync attempt failed")
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"AccessDeviceSyncLog: Device={self.device.device_id if self.device else None} | Success={self.success} | Timestamp={self.timestamp}"

class TamperEventLog(models.Model):
    EVENT_TYPES = (
        ("TAMPER_OPEN", "Tamper Open"),
        ("TAMPER_RESTORED", "Tamper Restored"),
        ("tamper_enabled", "Tamper Enabled"),
        ("tamper_disabled", "Tamper Disabled"),
    )
    
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES, help_text="Type of tamper event (e.g., open, restored, enabled, disabled)")
    access_control_device = models.ForeignKey(AccessControlDevice, on_delete=models.SET_NULL, null=True, blank=True, help_text="The access control device where this event occurred")
    event_timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"TamperEventLog: {self.event_type} at {self.event_timestamp}"

class MappingModalState(models.Model):
    """Model to track the status of the modal (open/closed)"""
    STATUSES = (
        ("open", "Open"),
        ("closed", "Closed"),
    )
    state = models.CharField(max_length=10, choices=STATUSES, default="closed")

    def __str__(self):
        return f"{str(self.state).upper()}"


class SystemMetadata(models.Model):
    """Model to store system metadata such as last successful balance fetch timestamp"""
    single_instance_id = models.CharField(max_length=10, unique=True, default="singleton", editable=False)
    last_successful_balance_fetch_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last successful balance fetch from external accounting system")
    last_successful_mapping_update_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last successful account mapping update")
    access_decision_cache = models.JSONField(null=True, blank=True, help_text="Cached access decisions for all users to optimize access checks")
    last_device_sync_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp of the last successful device sync with this system")
    # last_active_device_sync = models.CharField(max_length=100, null=True, blank=True, help_text="Device ID and Alias of the last successfully synced access control device")

    def __str__(self):
        return f"System Metadata: Last Successful Balance Fetch at {self.last_successful_balance_fetch_at}, Last Successful Mapping Update at {self.last_successful_mapping_update_at}, Last Device Sync at {self.last_device_sync_at}"


class Setting(SingletonModel):
    """Singleton model to store application settings"""

    AUTHORIZATION_FLOW_CHOICES = [
        ('grant_all', 'Grant All'),
        ('reject_all', 'Reject All'),
        ('check_usd_balance', 'Check USD Balance'),
        ('check_zwg_balance', 'Check ZWG Balance'),
        ('check_usd_or_zwg_balance', 'Check USD/ZWG Balance'),
    ]
    
    authorization_flow = models.CharField(max_length=30, choices=AUTHORIZATION_FLOW_CHOICES, default='check_usd_balance', help_text="Authorization flow to determine access decisions")
    usd_balance_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, null=True, blank=True, help_text="Minimum USD balance required for access")
    zwg_balance_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, null=True, blank=True, help_text="Minimum ZWG balance required for access")

    def __str__(self):
        return f": authorization_flow={self.authorization_flow}, usd_balance_threshold={self.usd_balance_threshold}, zwg_balance_threshold={self.zwg_balance_threshold}"
