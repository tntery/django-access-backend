import django_filters
from django.db.models import Q

from .models import AccountMapping


class AccountMappingFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method='filter_search', label='Search')
    account_user_id = django_filters.CharFilter(lookup_expr='icontains', label='Palladium User ID')
    first_name = django_filters.CharFilter(lookup_expr='icontains')
    last_name = django_filters.CharFilter(lookup_expr='icontains')
    device_access_id = django_filters.CharFilter(lookup_expr='icontains', label='Access ID')
    connection_status = django_filters.ChoiceFilter(
        choices=(('connected', 'Connected'), ('not_connected', 'Not Connected')),
        method='filter_connection_status',
        label='Status',
    )

    class Meta:
        model = AccountMapping
        fields = ['search', 'account_user_id', 'first_name', 'last_name', 'device_access_id', 'connection_status']

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(account_user_id__icontains=value)
            | Q(first_name__icontains=value)
            | Q(last_name__icontains=value)
            | Q(device_access_id__icontains=value)
        )

    def filter_connection_status(self, queryset, name, value):
        if value == 'connected':
            return queryset.filter(device_access_id__isnull=False).exclude(device_access_id='')
        if value == 'not_connected':
            return queryset.filter(Q(device_access_id__isnull=True) | Q(device_access_id=''))
        return queryset