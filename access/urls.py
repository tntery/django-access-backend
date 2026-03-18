from django.urls import path
from .views import access_event, mapping_list, set_modal_state, poll_temp_mapping

urlpatterns = [
    path('api/access', access_event, name='access_event'),
    path('connections/', mapping_list, name='mapping_list'),
    path('set-modal-state', set_modal_state, name='set_modal_state'),
    path('poll-temp-mapping', poll_temp_mapping, name='poll_temp_mapping'),
]
