from django.conf import settings
from django.db import migrations


def seed_roles(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    app_label, model_name = settings.AUTH_USER_MODEL.split('.')
    User = apps.get_model(app_label, model_name)
    alias = schema_editor.connection.alias
    groups = {
        role: Group.objects.using(alias).get_or_create(name=f'Portal {label}')[0]
        for role, label in [('viewer', 'Viewer'), ('staff', 'Staff'), ('admin', 'Admin')]
    }
    for user in User.objects.using(alias).all().iterator():
        role = 'admin' if user.is_superuser else 'staff' if user.is_staff else 'viewer'
        user.groups.add(groups[role])


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('access', '0013_alter_accountmapping_usd_balance_and_more'),
    ]

    operations = [migrations.RunPython(seed_roles, migrations.RunPython.noop)]
