from django.urls import path
from .views import access_event, account_mapping_list, set_modal_state, poll_temp_mapping, confirm_mapping, settings_view

urlpatterns = [
    path('api/access', access_event, name='access_event'),
    path('connections/', account_mapping_list, name='account_mapping_list'),
    path('set-modal-state', set_modal_state, name='set_modal_state'),
    path('poll-temp-mapping', poll_temp_mapping, name='poll_temp_mapping'),
    path('confirm-mapping', confirm_mapping, name='confirm_mapping'),
    path('settings/', settings_view, name='settings'),
]
