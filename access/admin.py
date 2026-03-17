from django.contrib import admin

# Register your models here.
from .models import Mapping, TempMapping, AccessLog, ModalStatus

admin.site.register(Mapping)
admin.site.register(TempMapping)
admin.site.register(AccessLog)
admin.site.register(ModalStatus)