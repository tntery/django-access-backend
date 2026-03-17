from django.db import models

class Mapping(models.Model):
    """Model to store the mapping between access control device IDs and accounting system IDs"""
    access_id = models.CharField(max_length=50, unique=True, null=True, blank=True,help_text="Unique identifier from access control device")
    mapping_id = models.CharField(max_length=50, unique=True, help_text="Unique identifier from accounting system")
    mapping_system = models.CharField(max_length=50, help_text="Accounting system name")

class TempMapping(models.Model):
    """Model to temporarily store access attempts before mapping is confirmed"""
    access_id = models.CharField(max_length=50, unique=True, help_text="Unique identifier from access control device")
    timestamp = models.DateTimeField(auto_now_add=True)

class AccessLog(models.Model):
    """Model to log access attempts and their mapping status"""
    STATUSES = (
        ("grant", "Grant"),
        ("reject", "Reject"),
    )
    access_id = models.CharField(max_length=50, help_text="Unique identifier from access control device")
    mapping_id = models.CharField(max_length=50, null=True)
    mapping_system = models.CharField(max_length=50, help_text="Accounting system name")
    status = models.CharField(max_length=10, choices=STATUSES)
    timestamp = models.DateTimeField(auto_now_add=True)

class ModalStatus(models.Model):
    """Model to track the status of the modal (open/closed)"""
    STATUSES = (
        ("open", "Open"),
        ("closed", "Closed"),
    )
    status = models.CharField(max_length=10, choices=STATUSES, default="closed")

