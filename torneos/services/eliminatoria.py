"""Rondas de eliminación directa: llaves tras la fase de grupos y modalidad relámpago.

En una ronda con número impar de equipos uno descansa (pasa directo a la siguiente ronda); se registra
como `Descanso` de la fecha. Se evita repetir el descanso en el mismo campeonato mientras sea posible.
"""
import random

from django.db import transaction
from django.db.models import F

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
    for llave, (local, visitante) in enumerate(pares, start=1):
        Partido.objects.create(campeonato=campeonato, fecha=fecha, local=local, visitante=visitante,
                               estadio=local.estadio, llave=llave)
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


def _cuadro_clasico(parejas_de_grupos):
    """Cruce clásico de 2 clasificados por grupo, ya en el orden del cuadro (llave 1, 2, 3...).

    Cada pareja de grupos (A, B) juega 1ºA–2ºB y 1ºB–2ºA. Los primeros cruces de todas las parejas van en la
    mitad de arriba del cuadro y los segundos en la de abajo, así el 1º y el 2º de un mismo grupo quedan en mitades
    opuestas y sólo pueden reencontrarse en la final. Ej. con 4 grupos: 1A–2B, 1C–2D, 1B–2A, 1D–2C.
    """
    arriba = [(a[0], b[1]) for a, b in parejas_de_grupos]
    abajo = [(b[0], a[1]) for a, b in parejas_de_grupos]
    return arriba + abajo


def _orden_siembra(n):
    """Orden de los cabezas de serie en un cuadro estándar de `n` equipos (potencia de 2): 1 y 2 sólo se cruzan
    en la final, 1–4 sólo en semifinales, etc. Ej. n=8: [1, 8, 4, 5, 2, 7, 3, 6] -> 1v8, 4v5, 2v7, 3v6."""
    orden = [1, 2]
    while len(orden) < n:
        tam = len(orden) * 2
        orden = [x for s in orden for x in (s, tam + 1 - s)]
    return orden[:n]


def _cuadro_por_siembra(siembra):
    """Cruces de la primera ronda para equipos ya ordenados por cabeza de serie (el mejor primero)."""
    orden = _orden_siembra(len(siembra))
    return [(siembra[orden[i] - 1], siembra[orden[i + 1] - 1]) for i in range(0, len(orden), 2)]


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
    if n == 2 and len(nombres) % 2 == 0:
        pares = _cuadro_clasico([(clasificados[nombres[i]], clasificados[nombres[i + 1]])
                                 for i in range(0, len(nombres), 2)])
    else:
        # siembra por posición en el grupo (todos los 1º, luego los 2º...) y cuadro estándar
        siembra = [e for pos in range(n) for g in nombres for e in clasificados[g][pos:pos + 1]]
        pares = _cuadro_por_siembra(siembra)
    return _crear_ronda(campeonato, total, pares)


@transaction.atomic
def avanzar_eliminatoria(campeonato, confirmar_descanso=False, semilla=None):
    """Crea la siguiente ronda con los ganadores (y quien descansó), o cierra el campeonato tras la final.

    Si los equipos que avanzan son impares pide confirmar el descanso de uno de ellos.
    """
    ultima = campeonato.fechas.filter(fase__in=FASES_ELIM).order_by('-numero').first()
    if not ultima:
        raise FixtureError('Primero genere la eliminatoria.')
    # los cruces siguen la posición en el cuadro (llave), no la hora programada de cada partido
    partidos = list(ultima.partidos.select_related('local', 'visitante')
                    .order_by(F('llave').asc(nulls_last=True), 'id'))
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
