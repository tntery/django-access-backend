from django import forms
from django.forms import ModelForm, ValidationError
from .models import AccessControlDevice, AccessId, AccountMapping, Setting


class ManualMappingForm(ModelForm):
    class Meta:
        model = AccountMapping
        fields = ['first_name', 'last_name', 'narration', 'classification',
                  'usd_balance', 'zwg_balance', 'access_override']
        labels = {'narration': 'Display name', 'classification': 'Staff / Member'}

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(field) for field in ('first_name', 'last_name', 'narration')):
            raise ValidationError('Enter a display name, first name, or last name.')
        return cleaned


class MappingClassificationForm(ModelForm):
    class Meta:
        model = AccountMapping
        fields = ['classification']
        labels = {'classification': 'Staff / Member'}


class ManualAccessIdForm(forms.Form):
    access_id = forms.CharField(max_length=100)
    auth_type = forms.ChoiceField(choices=AccessId.AUTH_TYPES)
    access_device = forms.ModelChoiceField(
        queryset=AccessControlDevice.objects.all(), required=False,
        help_text='Leave blank for a shared ID. Select a device for device-specific enrollment.',
    )

    def __init__(self, *args, mapping, instance=None, **kwargs):
        self.instance = instance
        if instance and 'initial' not in kwargs:
            kwargs['initial'] = {
                'access_id': instance.access_id, 'auth_type': instance.auth_type,
                'access_device': instance.access_device_id,
            }
        super().__init__(*args, **kwargs)
        self.mapping = mapping

    def clean(self):
        cleaned = super().clean()
        access_id = cleaned.get('access_id')
        device = cleaned.get('access_device')
        if not access_id:
            return cleaned
        conflicts = AccessId.objects.filter(access_id=access_id)
        if self.instance:
            conflicts = conflicts.exclude(pk=self.instance.pk)
        if device:
            conflicts = conflicts.filter(access_device=device) | conflicts.filter(access_device__isnull=True)
        # A shared ID must not shadow an enrollment on any device.
        active_conflicts = conflicts.filter(is_enabled=True).exclude(
            mapping__isnull=True, access_device=device,
        )
        if active_conflicts.exists():
            raise ValidationError('This access ID is already connected in this device scope. Edit or remove its existing connection first.')
        return cleaned


class SettingForm(ModelForm):
    class Meta:
        model = Setting
        fields = ['authorization_flow', 'usd_balance_threshold', 'zwg_balance_threshold']

    def clean(self):
        cleaned_data = super().clean()
        authorization_flow = cleaned_data.get('authorization_flow')
        usd_balance_threshold = cleaned_data.get('usd_balance_threshold')
        zwg_balance_threshold = cleaned_data.get('zwg_balance_threshold')

        match authorization_flow:
            case 'grant_all':
                cleaned_data['usd_balance_threshold'] = None  # Clear USD threshold if not relevant
                cleaned_data['zwg_balance_threshold'] = None  # Clear ZWG threshold if not relevant
            case 'reject_all':
                cleaned_data['usd_balance_threshold'] = None  # Clear USD threshold if not relevant
                cleaned_data['zwg_balance_threshold'] = None  # Clear ZWG threshold if not relevant
            case 'check_usd_balance':
                cleaned_data['zwg_balance_threshold'] = None  # Clear ZWG threshold if not relevant
                if usd_balance_threshold is None:
                    raise ValidationError('USD balance threshold cannot be empty when using "Check USD Balance" flow.')
            case 'check_zwg_balance':
                cleaned_data['usd_balance_threshold'] = None  # Clear USD threshold if not relevant
                if zwg_balance_threshold is None:
                    raise ValidationError('ZWG balance threshold cannot be empty when using "Check ZWG Balance" flow.')

            case 'check_usd_or_zwg_balance':
                # both threshholds are required.
                if usd_balance_threshold is None or zwg_balance_threshold is None:
                    raise ValidationError('Both USD and ZWG balance thresholds must be set when using "Check USD/ZWG Balance" flow.')
        