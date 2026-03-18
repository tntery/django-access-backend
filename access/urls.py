from django.urls import path
from .views import access_event, mapping_list, set_modal_status, poll_temp_mapping

urlpatterns = [
    path('api/access', access_event, name='access_event'),
    path('connections/', mapping_list, name='mapping_list'),
    path('set-modal-status', set_modal_status, name='set_modal_status'),
    path('poll-temp-mapping', poll_temp_mapping, name='poll_temp_mapping'),
]
