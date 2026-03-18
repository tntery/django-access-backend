from django.db import models
from solo.models import SingletonModel

class AccountMapping(models.Model):
    """Model to store the mapping between access control device IDs and accounting system IDs"""

    ACCOUNTING_SYSTEM_CHOICES = (
        ("palladium", "Palladium"),
        ("quickbooks", "QuickBooks"),
        ("xero", "Xero"),
    )
    device_access_id = models.CharField(max_length=50, unique=True, null=True, blank=True, help_text="Unique identifier from access control device")
    account_user_id = models.CharField(max_length=50, unique=True, help_text="Unique identifier from accounting system")
    accounting_system = models.CharField(max_length=50, choices=ACCOUNTING_SYSTEM_CHOICES, default="palladium", help_text="Accounting system name")
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Account-{self.account_user_id} | Access-{self.device_access_id} | System-{self.accounting_system}"

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
    device_access_id = models.CharField(max_length=50, help_text="Unique identifier from access control device")
    account_user_id = models.CharField(max_length=50, null=True)
    accounting_system = models.CharField(max_length=50, help_text="Accounting system name")
    access_status = models.CharField(max_length=10, choices=ACCESS_STATUSES)
    event_timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"AccessEventLog: {self.device_access_id} | {self.account_user_id} | {self.accounting_system} | {self.access_status} at {self.event_timestamp}"

class MappingModalState(models.Model):
    """Model to track the status of the modal (open/closed)"""
    STATUSES = (
        ("open", "Open"),
        ("closed", "Closed"),
    )
    state = models.CharField(max_length=10, choices=STATUSES, default="closed")

    def __str__(self):
        return f"{str(self.state).upper()}"
    
class Setting(SingletonModel):
    """Singleton model to store application """

    AUTHORIZATION_FLOW_CHOICES = [
        ('grant_all', 'Grant All'),
        ('reject_all', 'Reject All'),
        ('check_balance', 'Check Balance'),
    ]
    
    authorization_flow = models.CharField(max_length=20, choices=AUTHORIZATION_FLOW_CHOICES, default='check_balance', help_text="Authorization flow to determine access decisions")
    balance_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, null=True, blank=True, help_text="Minimum balance required for access")

    def __str__(self):
        return f": authorization_flow={self.authorization_flow}, balance_threshold={self.balance_threshold}"

