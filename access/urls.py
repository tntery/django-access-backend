from django.urls import path
from .views import access_event

urlpatterns = [
    path('api/access', access_event, name='access_event'),
]