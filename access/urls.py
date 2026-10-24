from django.shortcuts import redirect
from django.urls import path
from .views import (
    AccessLoginView,
    AccessLogoutView,
    access_event_view,
    account_mapping_list_view,
    account_mapping_detail_view,
    account_mapping_override_view,
    set_modal_state_view,
    api_account_mapping_view,
    settings_view,
)

urlpatterns = [
    # authentication
    path('login/', AccessLoginView.as_view(), name='login'),
    path('logout/', AccessLogoutView.as_view(), name='logout'),

    # UI views
    path('', account_mapping_list_view, name='account_mapping_list'),
    path('mappings/<int:pk>/', account_mapping_detail_view, name='account_mapping_detail'),
    path('mappings/<int:pk>/access-override/', account_mapping_override_view, name='account_mapping_override'),
    path('settings/', settings_view, name='settings'),
    
    # api endpoints
    path('api/access/<str:device_serial_number>', access_event_view, name='access_event'),
    path('api/modal-state', set_modal_state_view, name='set_modal_state'),
    path('api/mappings/<str:filter_name>', api_account_mapping_view, name='api_account_mapping_view_pending'),
    path('api/mappings', api_account_mapping_view, name='api_account_mapping_view'),

    path('favicon.ico', lambda request: redirect('static/access/img/favicon.png')),  # return static img favicon for favicon requests to avoid unnecessary 404 errors in logs

]
