from rest_framework.permissions import SAFE_METHODS, BasePermission

ADMIN = 'Administrador'
OPERADOR = 'Operador'
ORGANIZADOR = 'Organizador'
AFICIONADO = 'Aficionado'   # cuenta pública: sin acceso al panel, sólo intereses y notificaciones


def tiene_rol(user, *roles):
    if not (user and user.is_authenticated):
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=roles).exists()


class LecturaPublicaEscrituraStaff(BasePermission):
    """Cualquiera lee; sólo escriben los roles permitidos por la vista.

    Cada vista puede definir `roles_escritura` (por defecto sólo Administrador). El Operador
    sólo tiene acceso de escritura en partidos y eventos (resultados en vivo).
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        roles = getattr(view, 'roles_escritura', (ADMIN,))
        return tiene_rol(request.user, *roles)


class SoloAdministrador(BasePermission):
    def has_permission(self, request, view):
        return tiene_rol(request.user, ADMIN)


class AdministradorOOrganizador(BasePermission):
    """Gestión de usuarios: el Administrador ve todos; el Organizador sólo los operadores que creó."""

    def has_permission(self, request, view):
        return tiene_rol(request.user, ADMIN, ORGANIZADOR)
