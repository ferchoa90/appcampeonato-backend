"""Alcance de datos por rol.

- Administrador: ve y hace todo (incluye lo «del sistema», sin propietario).
- Organizador: sólo sus campeonatos y sus equipos (y lo que cuelga de ellos).
- Operador sin restricción (los de antes): opera todo, como siempre. Operador restringido (creado por un
  organizador): sólo los campeonatos que se le asignaron.
- Anónimo: sólo lo publicado.

Los filtros se aplican en el servidor (querysets): lo ajeno aparece como inexistente (404), no sólo oculto en pantalla.
"""
from django.db.models import Q

from torneos.models import Campeonato, Equipo, PerfilUsuario
from torneos.permissions import ADMIN, OPERADOR, ORGANIZADOR, tiene_rol


def es_admin(user):
    return tiene_rol(user, ADMIN)


def es_organizador(user):
    return bool(user and user.is_authenticated and not es_admin(user)
                and user.groups.filter(name=ORGANIZADOR).exists())


def es_operador(user):
    return bool(user and user.is_authenticated and not es_admin(user) and not es_organizador(user)
                and user.groups.filter(name=OPERADOR).exists())


def es_usuario_de_gestion(user):
    return es_admin(user) or es_organizador(user) or es_operador(user)


def perfil_de(user):
    return PerfilUsuario.objects.get_or_create(user=user)[0]


def campeonatos_gestionables(user):
    """Campeonatos que el usuario puede ver y administrar en el panel."""
    if es_admin(user):
        return Campeonato.objects.all()
    if es_organizador(user):
        return Campeonato.objects.filter(propietario=user)
    if es_operador(user):
        perfil = perfil_de(user)
        return perfil.campeonatos.all() if perfil.restringido else Campeonato.objects.all()
    return Campeonato.objects.none()


def alcance_campeonatos(user):
    """Con sesión de gestión: lo que gestiona. Sin ella (o sin rol): sólo lo publicado."""
    if es_usuario_de_gestion(user):
        return campeonatos_gestionables(user)
    return Campeonato.objects.filter(publicado=True)


def equipos_gestionables(user):
    if es_admin(user):
        return Equipo.objects.all()
    if es_organizador(user):
        return Equipo.objects.filter(propietario=user)
    if es_operador(user):
        perfil = perfil_de(user)
        if not perfil.restringido:
            return Equipo.objects.all()
        return Equipo.objects.filter(inscripciones__campeonato__in=perfil.campeonatos.all()).distinct()
    return Equipo.objects.none()


def alcance_equipos(user):
    if es_usuario_de_gestion(user):
        return equipos_gestionables(user)
    return Equipo.objects.filter(inscripciones__campeonato__publicado=True).distinct()


def puede_usar_equipo(user, equipo, campeonato=None):
    """¿Puede `user` usar este equipo (inscribirlo, programarlo)? Debe ser del mismo dueño que el campeonato."""
    if campeonato is not None and equipo.propietario_id != campeonato.propietario_id:
        return False
    if es_admin(user):
        return True
    return equipo.propietario_id == user.id


def filtro_por_campeonato(qs, user, campo='campeonato'):
    """Restringe un queryset a los campeonatos del alcance del usuario."""
    return qs.filter(**{f'{campo}__in': alcance_campeonatos(user)})
