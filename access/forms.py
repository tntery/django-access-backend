from django.forms import ModelForm, ValidationError
from .models import Setting


class SettingForm(ModelForm):
    class Meta:
        model = Setting
        fields = ['authorization_flow', 'balance_threshold',]

    def clean(self):
        cleaned_data = super().clean()
        authorization_flow = cleaned_data.get('authorization_flow')
        balance_threshold = cleaned_data.get('balance_threshold')

        if authorization_flow == 'check_balance' and balance_threshold is None:

            raise ValidationError('Balance threshold cannot be empty when using "Check Balance" flow.')