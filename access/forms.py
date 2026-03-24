from django.forms import ModelForm, ValidationError
from .models import Setting


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
                if usd_balance_threshold is None and zwg_balance_threshold is None:
                    raise ValidationError('At least one of USD or ZWG balance thresholds must be set when using "Check USD/ZWG Balance" flow.')