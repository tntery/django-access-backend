from django.contrib import admin

# Register your models here.
from .models import AccountMapping, PendingAccountMapping, AccessEventLog, MappingModalState, Setting

admin.site.register(AccountMapping)
admin.site.register(PendingAccountMapping)
admin.site.register(AccessEventLog)
admin.site.register(MappingModalState)
admin.site.register(Setting)
