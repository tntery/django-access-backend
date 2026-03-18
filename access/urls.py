from django.urls import path
from .views import access_event, mapping_list

urlpatterns = [
    path('api/access', access_event, name='access_event'),
    path('connections/', mapping_list, name='mapping_list'),
]