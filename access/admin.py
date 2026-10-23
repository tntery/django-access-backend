from django.contrib import admin

# Register your models here.
from .models import AccessDeviceSyncLog, AccessId, AccessControlDevice, AccountMapping, PendingAccountMapping, AccessEventLog, MappingModalState, Setting, SystemMetadata, TamperEventLog

class AccessIdAdmin(admin.ModelAdmin):
    list_display = ('access_id', 'auth_type', 'access_device__alias', 'mapping', 'created_at', 'last_updated_at')
    search_fields = ('access_id', 'auth_type', 'mapping__first_name', 'mapping__last_name', 'mapping__narration')
    list_filter = ('auth_type', 'created_at', 'last_updated_at')

class AccessControlDeviceAdmin(admin.ModelAdmin):
    list_display = ('device_id', 'alias', 'location', 'is_active', 'last_sync_at')
    search_fields = ('device_id', 'alias', 'location')
    list_filter = ('is_active', 'last_sync_at')

class AccountMappingAdmin(admin.ModelAdmin):
    list_display = ('account_user_id', 'narration', 'usd_balance', 'zwg_balance', 'created_at', 'last_updated_at')
    search_fields = ('account_user_id', 'first_name', 'last_name', 'narration', 'accounting_system')
    list_filter = ('accounting_system', 'created_at', 'last_updated_at')

class AccessEventLogAdmin(admin.ModelAdmin):
    list_display = ('device_access_id', 'user__narration', 'access_type', 'access_status',  'access_control_device__alias','event_timestamp')
    search_fields = ('device_access_id', 'user__account_user_id', 'user__first_name', 'user__last_name', 'user__narration', 'access_status', 'access_type', 'access_control_device__alias', 'access_control_device__device_id')
    list_filter = ('access_status', 'access_type', 'event_timestamp', 'access_control_device__alias')

class AccessDeviceSyncLogAdmin(admin.ModelAdmin):
    list_display = ('device', 'success', 'timestamp')
    search_fields = ('device__alias', 'device__device_id')
    list_filter = ('success', 'timestamp', 'device__alias')

class TamperEventLogAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'event_timestamp')
    search_fields = ('event_type',)
    list_filter = ('event_type', 'event_timestamp')

admin.site.register(AccountMapping, AccountMappingAdmin)
admin.site.register(AccessId, AccessIdAdmin)
admin.site.register(AccessControlDevice, AccessControlDeviceAdmin)
admin.site.register(PendingAccountMapping)
admin.site.register(AccessEventLog, AccessEventLogAdmin)
admin.site.register(MappingModalState)
admin.site.register(TamperEventLog, TamperEventLogAdmin)
admin.site.register(SystemMetadata)
admin.site.register(Setting)
admin.site.register(AccessDeviceSyncLog, AccessDeviceSyncLogAdmin)
