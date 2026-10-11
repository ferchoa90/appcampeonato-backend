"""Rondas de eliminación directa: llaves tras la fase de grupos y modalidad relámpago.

En una ronda con número impar de equipos uno descansa (pasa directo a la siguiente ronda); se registra
como `Descanso` de la fecha. Se evita repetir el descanso en el mismo campeonato mientras sea posible.
"""
import random

from django.db import transaction

from torneos.models import Descanso, Fecha, Partido
from torneos.services.fixture import FixtureError, RequiereConfirmacion, validar_primera_fecha
from torneos.services.tabla import calcular_tabla

FASE_POR_TAMANO = {2: Fecha.Fase.FINAL, 4: Fecha.Fase.SEMIFINAL, 8: Fecha.Fase.CUARTOS, 16: Fecha.Fase.OCTAVOS}
NOMBRE_FASE = {
    Fecha.Fase.FINAL: 'Final', Fecha.Fase.SEMIFINAL: 'Semifinales',
    Fecha.Fase.CUARTOS: 'Cuartos de final', Fecha.Fase.OCTAVOS: 'Octavos de final',
    Fecha.Fase.RONDA: 'Ronda eliminatoria',
}
FASES_ELIM = list(NOMBRE_FASE)


def _siguiente_numero(campeonato):
    ultima = campeonato.fechas.order_by('-numero').first()
    return (ultima.numero + 1) if ultima else 1


def _crear_ronda(campeonato, n_equipos, pares, descansa=None):
    """Crea la ronda SIN día ni hora: el administrador los elige partido a partido cuando termina la
    ronda anterior (ver `FechaViewSet.programar`)."""
    fase = FASE_POR_TAMANO.get(n_equipos, Fecha.Fase.RONDA)
    nombre = NOMBRE_FASE[fase] if fase != Fecha.Fase.RONDA else f'Ronda de {n_equipos} equipos'
    fecha = Fecha.objects.create(campeonato=campeonato, fase=fase, numero=_siguiente_numero(campeonato),
                                 nombre=nombre)
    for local, visitante in pares:
        Partido.objects.create(campeonato=campeonato, fecha=fecha, local=local, visitante=visitante,
                               estadio=local.estadio)
    if descansa:
        Descanso.objects.create(fecha=fecha, equipo=descansa)
    return fecha


def _emparejar(equipos, rnd, ya_descanso=()):
    """Empareja en orden. Con cantidad impar saca un equipo que descansa (pasa directo)."""
    equipos = list(equipos)
    descansa = None
    if len(equipos) % 2:
        candidatos = [e for e in equipos if e.id not in ya_descanso] or equipos
        descansa = rnd.choice(candidatos)
        equipos.remove(descansa)
    return [(equipos[i], equipos[i + 1]) for i in range(0, len(equipos), 2)], descansa


def _ya_descansaron(campeonato):
    return set(Descanso.objects.filter(fecha__campeonato=campeonato, fecha__fase__in=FASES_ELIM)
               .values_list('equipo_id', flat=True))


@transaction.atomic
def generar_relampago(campeonato, equipos, confirmar_descanso=False, semilla=None, primera_fecha=None):
    """Primera ronda de un torneo relámpago: sorteo de todos los equipos inscritos.

    Con `primera_fecha` los cruces de esta ronda los elige el usuario (ver fixture.validar_primera_fecha).
    """
    equipos = list(equipos)
    if len(equipos) < 2:
        raise FixtureError('Inscriba al menos 2 equipos antes de sortear.')
    if len(equipos) % 2 and not confirmar_descanso:
        raise RequiereConfirmacion(
            f'Hay {len(equipos)} equipos (número impar): un equipo descansará esta fecha y pasará directo '
            'a la siguiente ronda. ¿Desea continuar?')
    if primera_fecha:
        pares, descansa = validar_primera_fecha(equipos, primera_fecha)
    else:
        rnd = random.Random(semilla)
        rnd.shuffle(equipos)  # el sorteo
        pares, descansa = _emparejar(equipos, rnd)
    return _crear_ronda(campeonato, len(equipos), pares, descansa)


@transaction.atomic
def generar_eliminatoria(campeonato):
    """Llaves tras la fase de grupos con los mejores de cada grupo."""
    if campeonato.formato != campeonato.Formato.GRUPOS:
        raise FixtureError('Sólo los campeonatos con fase de grupos tienen eliminatoria.')
    if campeonato.fechas.filter(fase__in=FASES_ELIM).exists():
        raise FixtureError('La eliminatoria ya fue generada.')
    if campeonato.partidos.exclude(estado=Partido.Estado.FINALIZADO).filter(fecha__fase=Fecha.Fase.GRUPOS).exists():
        raise FixtureError('Aún hay partidos pendientes en la fase de grupos.')

    n = campeonato.clasificados_por_grupo
    clasificados = {}  # nombre_grupo -> [equipos en orden]
    for grupo in campeonato.grupos.all():
        clasificados[grupo.nombre] = [f['equipo'] for f in calcular_tabla(campeonato, grupo)[:n]]
    total = sum(len(v) for v in clasificados.values())
    if total not in FASE_POR_TAMANO:
        raise FixtureError(f'{total} clasificados no forman una llave válida (use 2, 4, 8 o 16).')

    nombres = sorted(clasificados)
    pares = []
    if n == 2 and len(nombres) % 2 == 0:
        # cruce clásico: 1º del grupo A vs 2º del B, 1º del B vs 2º del A...
        for i in range(0, len(nombres), 2):
            a, b = clasificados[nombres[i]], clasificados[nombres[i + 1]]
            pares.append((a[0], b[1]))
            pares.append((b[0], a[1]))
    else:
        # siembra por posición en el grupo: mejores contra peores
        orden = [e for pos in range(n) for g in nombres for e in clasificados[g][pos:pos + 1]]
        while orden:
            pares.append((orden.pop(0), orden.pop()))
    return _crear_ronda(campeonato, total, pares)


@transaction.atomic
def avanzar_eliminatoria(campeonato, confirmar_descanso=False, semilla=None):
    """Crea la siguiente ronda con los ganadores (y quien descansó), o cierra el campeonato tras la final.

    Si los equipos que avanzan son impares pide confirmar el descanso de uno de ellos.
    """
    ultima = campeonato.fechas.filter(fase__in=FASES_ELIM).order_by('-numero').first()
    if not ultima:
        raise FixtureError('Primero genere la eliminatoria.')
    partidos = list(ultima.partidos.select_related('local', 'visitante'))
    if any(p.estado != Partido.Estado.FINALIZADO for p in partidos):
        raise FixtureError('Hay partidos de la ronda actual sin finalizar.')
    avanzan = []
    for p in partidos:
        if p.ganador is None:
            raise FixtureError(f'{p} terminó empatado: registre los penales para definir el ganador.')
        avanzan.append(p.ganador)
    avanzan += [d.equipo for d in ultima.descansos.select_related('equipo')]  # pasaron directo

    if ultima.fase == Fecha.Fase.FINAL:
        campeonato.estado = campeonato.Estado.FINALIZADO
        campeonato.save(update_fields=['estado'])
        return None
    if len(avanzan) % 2 and not confirmar_descanso:
        raise RequiereConfirmacion(
            f'Avanzan {len(avanzan)} equipos (número impar): un equipo descansará la siguiente fecha y pasará '
            'directo a la ronda posterior. ¿Desea continuar?')
    rnd = random.Random(semilla)
    pares, descansa = _emparejar(avanzan, rnd, _ya_descansaron(campeonato))
    return _crear_ronda(campeonato, len(avanzan), pares, descansa)
