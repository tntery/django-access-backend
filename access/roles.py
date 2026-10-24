from django.core.exceptions import PermissionDenied


ROLE_CHOICES = [('viewer', 'Viewer'), ('staff', 'Staff'), ('admin', 'Admin')]
ROLE_GROUPS = {role: f'Portal {label}' for role, label in ROLE_CHOICES}


def get_portal_role(user):
    if not user.is_authenticated or not user.is_active:
        return None
    return get_assigned_role(user)


def get_assigned_role(user):
    if user.is_superuser:
        return 'admin'
    groups = {group.name for group in user.groups.all()}
    if ROLE_GROUPS['admin'] in groups:
        return 'admin'
    if ROLE_GROUPS['staff'] in groups or user.is_staff:
        return 'staff'
    return 'viewer'


def require_role(request, *roles):
    if get_portal_role(request.user) not in roles:
        raise PermissionDenied


def portal_permissions(request):
    role = get_portal_role(request.user)
    return {
        'portal_role': role,
        'can_manage_mappings': role in ('staff', 'admin'),
        'can_manage_users': role == 'admin',
    }
