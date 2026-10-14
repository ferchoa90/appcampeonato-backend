"""Sorteo de fechas (método del círculo / round-robin)."""
import random

from django.db import transaction

from torneos.models import Descanso, EventoPartido, Fecha, Grupo, Inscripcion, Partido
from torneos.services import horarios, programacion


class FixtureError(Exception):
    pass


class RequiereConfirmacion(FixtureError):
    """Hay grupos con número impar de equipos: alguien descansa cada fecha."""

    def __init__(self, detalle):
        super().__init__(detalle)
        self.detalle = detalle


def validar_primera_fecha(equipos, primera):
    """Valida los partidos de la primera fecha elegidos a mano.

    `equipos`: Equipos inscritos; `primera`: lista de {'local': id, 'visitante': id}.
    Devuelve ([(local, visitante)], descansa|None). Deben participar todos los equipos
    (con cantidad impar, uno queda sin partido y descansa esa fecha).
    """
    por_id = {e.id: e for e in equipos}
    pares, usados = [], set()
    try:
        for p in primera:
            local, visitante = int(p['local']), int(p['visitante'])
            if local not in por_id or visitante not in por_id:
                raise FixtureError('La primera fecha incluye equipos que no están inscritos en el campeonato.')
            if local == visitante:
                raise FixtureError('Un equipo no puede jugar contra sí mismo.')
            if usados & {local, visitante}:
                repetido = por_id[(usados & {local, visitante}).pop()]
                raise FixtureError(f'{repetido} aparece más de una vez en la primera fecha.')
            usados |= {local, visitante}
            pares.append((por_id[local], por_id[visitante]))
    except (KeyError, TypeError, ValueError):
        raise FixtureError('Primera fecha inválida: use una lista de {local, visitante} con ids de equipos.')
    esperados = len(equipos) // 2
    if len(pares) != esperados:
        raise FixtureError(f'La primera fecha debe tener {esperados} partidos para {len(equipos)} equipos '
                           f'(tiene {len(pares)}).')
    descansa = None
    if len(equipos) % 2:
        descansa = por_id[(set(por_id) - usados).pop()]
    return pares, descansa


def circulo(equipos, primera=None):
    """Devuelve una lista de rondas; cada ronda es (lista de (local, visitante), descansa).

    Con número impar se añade un 'fantasma' (None): quien le toque descansa.
    Alterna localías para equilibrarlas. Con `primera=(pares, descansa)` la primera ronda queda
    fija (partidos elegidos a mano) y las demás completan el todos contra todos sin repetir cruces:
    se colocan las parejas elegidas en posiciones enfrentadas del círculo.
    """
    if primera:
        pares_fijos, descansa_fija = primera
        izquierda = [a for a, _ in pares_fijos]
        derecha = [b for _, b in pares_fijos]
        if descansa_fija is not None:
            izquierda.append(descansa_fija)
            derecha.append(None)
        lista = izquierda + derecha[::-1]
    else:
        lista = list(equipos)
    if len(lista) < 2:
        return []
    if len(lista) % 2:
        lista.append(None)
    n = len(lista)
    rondas = []
    for r in range(n - 1):
        pares, descansa = [], None
        for i in range(n // 2):
            a, b = lista[i], lista[n - 1 - i]
            if a is None or b is None:
                descansa = b if a is None else a
                continue
            # alternar localía por ronda y por posición
            pares.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rondas.append((pares, descansa))
        lista = [lista[0]] + [lista[-1]] + lista[1:-1]
    if primera:  # respeta la localía elegida a mano en la primera fecha
        rondas[0] = (list(primera[0]), primera[1])
    return rondas


def grupos_impares(campeonato):
    """Lista de (nombre_grupo, cantidad) de grupos con cantidad impar de equipos."""
    impares = []
    for etiqueta, inscripciones in _particiones(campeonato):
        if len(inscripciones) % 2:
            impares.append((etiqueta, len(inscripciones)))
    return impares


def _particiones(campeonato):
    """Divide las inscripciones en grupos de sorteo: [(etiqueta, [inscripcion...])]."""
    inscripciones = list(Inscripcion.objects.filter(campeonato=campeonato).select_related('equipo', 'grupo'))
    if campeonato.formato == campeonato.Formato.LIGA:
        return [('Liga', inscripciones)] if inscripciones else []
    por_grupo = {}
    for ins in inscripciones:
        if ins.grupo_id is None:
            raise FixtureError(f'El equipo {ins.equipo} no tiene grupo asignado. Sortee o asigne los grupos primero.')
        por_grupo.setdefault(ins.grupo, []).append(ins)
    return sorted(((g.nombre, lst) for g, lst in por_grupo.items()), key=lambda x: x[0])


def sorteo_bloqueado(campeonato):
    """Motivo (texto) si el sorteo ya no se puede repetir, o None si todavía se puede.

    El sorteo se puede rehacer mientras ningún partido haya comenzado: un partido iniciado, en
    entretiempo o finalizado (o con eventos registrados) lo bloquea. Pendientes y postergados no.
    """
    E = Partido.Estado
    if campeonato.partidos.filter(estado__in=[E.INICIADO, E.ENTRETIEMPO, E.SEGUNDO_TIEMPO, E.FINALIZADO]).exists() \
            or EventoPartido.objects.filter(partido__campeonato=campeonato).exists():
        return 'Ya hay partidos iniciados o jugados: el sorteo no se puede repetir.'
    return None


@transaction.atomic
def sortear_grupos(campeonato, semilla=None):
    """Reparte al azar los equipos inscritos en `campeonato.num_grupos` grupos (A, B, C...).

    Si ya había un fixture y ningún partido comenzó, se descarta (los grupos cambian).
    """
    if campeonato.partidos.exists():
        bloqueo = sorteo_bloqueado(campeonato)
        if bloqueo:
            raise FixtureError(bloqueo)
        campeonato.partidos.all().delete()
        campeonato.fechas.all().delete()
    n = campeonato.num_grupos
    inscripciones = list(campeonato.inscripciones.all())
    if n < 1 or len(inscripciones) < n * 2:
        raise FixtureError(f'Se necesitan al menos {n * 2} equipos inscritos para {n} grupo(s).')
    rnd = random.Random(semilla)
    rnd.shuffle(inscripciones)
    campeonato.grupos.all().delete()
    grupos = [Grupo.objects.create(campeonato=campeonato, nombre=chr(ord('A') + i)) for i in range(n)]
    for i, ins in enumerate(inscripciones):
        ins.grupo = grupos[i % n]
        ins.save(update_fields=['grupo'])
    return grupos


@transaction.atomic
def limpiar_sorteo(campeonato):
    """Deshace por completo el sorteo: elimina partidos, fechas (con sus descansos) y grupos.

    Se conservan los equipos inscritos y las preferencias de programación. El campeonato vuelve a
    Borrador. Sólo posible mientras ningún partido haya comenzado. Devuelve cuánto se eliminó.
    """
    bloqueo = sorteo_bloqueado(campeonato)
    if bloqueo:
        raise FixtureError(f'No se puede limpiar. {bloqueo}')
    resumen = {'partidos': campeonato.partidos.count(), 'fechas': campeonato.fechas.count(),
               'grupos': campeonato.grupos.count()}
    campeonato.partidos.all().delete()
    campeonato.fechas.all().delete()
    campeonato.grupos.all().delete()  # las inscripciones quedan sin grupo (SET_NULL)
    campeonato.estado = campeonato.Estado.BORRADOR
    campeonato.modalidad = campeonato.Modalidad.IDA
    campeonato.ida_vuelta = False
    campeonato.save(update_fields=['estado', 'modalidad', 'ida_vuelta'])
    return resumen


def resolver_modalidad(campeonato, modalidad=None, ida_vuelta=None):
    """Modalidad elegida; admite el parámetro antiguo `ida_vuelta` (booleano)."""
    M = campeonato.Modalidad
    if modalidad:
        if modalidad not in M.values:
            raise FixtureError(f'Modalidad inválida. Use: {", ".join(M.values)}')
        return modalidad
    if ida_vuelta is not None:
        return M.IDA_VUELTA if ida_vuelta else M.IDA
    return campeonato.modalidad


@transaction.atomic
def generar_fixture(campeonato, ida_vuelta=None, confirmar_descanso=False, semilla=None,
                    fecha_inicio=None, dias_entre_fechas=None, regenerar=False, modalidad=None,
                    dias_semana=None, partidos_por_dia=None, hora_inicio=None, minutos_entre_partidos=None,
                    primera_fecha=None, feriados=None, duracion_tiempo=None):
    """Sortea y crea Fechas y Partidos según la modalidad: SOLO IDA, IDA Y VUELTA o RELÁMPAGO.

    Si hay equipos impares y `confirmar_descanso` es False, lanza RequiereConfirmacion para que la
    interfaz pregunte al usuario antes de continuar. En RELÁMPAGO sólo se sortea la primera ronda;
    las siguientes se crean con `avanzar_eliminatoria`.

    Con `fecha_inicio` se asigna día y hora a los partidos según `dias_semana` (1=lunes..7=domingo),
    `partidos_por_dia`, `hora_inicio` y `minutos_entre_partidos` (ver services.programacion). Los
    parámetros no indicados conservan lo guardado en el campeonato.

    `primera_fecha` (opcional, [{'local': id, 'visitante': id}]) fija a mano los partidos de la fecha 1;
    el resto se sortea sin repetir cruces. Sólo en campeonatos sin grupos.

    `feriados` ('omitir' | 'jugar'): qué hacer si algún partido cae en feriado. Sin decidir y con
    conflicto se lanza `programacion.ConflictoFeriados` y no se cambia nada (la transacción se revierte).
    """
    try:
        programacion.aplicar_configuracion(campeonato, dias_semana, partidos_por_dia, hora_inicio,
                                           minutos_entre_partidos, dias_entre_fechas, duracion_tiempo)
    except programacion.ProgramacionError as e:
        raise FixtureError(str(e))
    modalidad = resolver_modalidad(campeonato, modalidad, ida_vuelta)
    ida_vuelta = modalidad == campeonato.Modalidad.IDA_VUELTA
    relampago = modalidad == campeonato.Modalidad.RELAMPAGO
    if relampago and campeonato.formato != campeonato.Formato.LIGA:
        raise FixtureError('La modalidad relámpago aplica a campeonatos sin grupos.')
    if primera_fecha and campeonato.formato != campeonato.Formato.LIGA:
        raise FixtureError('La primera fecha manual aplica a campeonatos sin grupos.')
    if campeonato.partidos.exists():
        if not regenerar:
            raise FixtureError('El campeonato ya tiene un fixture generado. Use regenerar=true para reemplazarlo.')
        bloqueo = sorteo_bloqueado(campeonato)
        if bloqueo:
            raise FixtureError(f'No se puede regenerar. {bloqueo}')
        campeonato.partidos.all().delete()
        campeonato.fechas.all().delete()

    def _cerrar(fechas):
        if campeonato.estado == campeonato.Estado.BORRADOR:
            campeonato.estado = campeonato.Estado.EN_CURSO
        campeonato.modalidad = modalidad
        campeonato.ida_vuelta = ida_vuelta
        campeonato.save(update_fields=['estado', 'modalidad', 'ida_vuelta'])
        programacion.programar_inicial(campeonato, fechas, fecha_inicio, feriados)
        horarios.aplicar_horarios_fijos(campeonato)  # reglas de logística guardadas (p. ej. BODEGA siempre 2.º horario)
        return fechas

    if relampago:
        from torneos.services.eliminatoria import generar_relampago
        equipos = [i.equipo for i in Inscripcion.objects.filter(campeonato=campeonato).select_related('equipo')]
        fecha = generar_relampago(campeonato, equipos, confirmar_descanso, semilla, primera_fecha)
        return _cerrar([fecha])

    particiones = _particiones(campeonato)
    if not particiones or all(len(p) < 2 for _, p in particiones):
        raise FixtureError('Inscriba al menos 2 equipos antes de generar el fixture.')

    impares = [(e, len(p)) for e, p in particiones if len(p) % 2]
    if impares and not confirmar_descanso:
        detalle = ', '.join(f'{e} ({c} equipos)' for e, c in impares)
        raise RequiereConfirmacion(
            f'Número impar de equipos en: {detalle}. Un equipo descansará en cada fecha. ¿Desea continuar?')

    rnd = random.Random(semilla)
    rondas_por_grupo = []
    for etiqueta, inscripciones in particiones:
        equipos = [i.equipo for i in inscripciones]
        grupo = inscripciones[0].grupo
        if primera_fecha:
            pares, descansa = validar_primera_fecha(equipos, primera_fecha)
            # el orden de las parejas decide cómo se reparten las demás fechas: se sortea
            posiciones = list(pares)
            rnd.shuffle(posiciones)
            rondas = circulo(equipos, (posiciones, descansa))
        else:
            rnd.shuffle(equipos)  # el sorteo
            rondas = circulo(equipos)
        rondas_por_grupo.append((grupo, rondas))

    total_rondas = max(len(r) for _, r in rondas_por_grupo)
    fase = Fecha.Fase.LIGA if campeonato.formato == campeonato.Formato.LIGA else Fecha.Fase.GRUPOS
    numeros = total_rondas * (2 if ida_vuelta else 1)
    fechas = []
    for idx in range(numeros):
        fechas.append(Fecha.objects.create(
            campeonato=campeonato, fase=fase, numero=idx + 1, nombre=f'Fecha {idx + 1}'))

    for grupo, rondas in rondas_por_grupo:
        for idx, (pares, descansa) in enumerate(rondas):
            fecha = fechas[idx]
            for local, visitante in pares:
                Partido.objects.create(campeonato=campeonato, fecha=fecha, grupo=grupo, local=local,
                                       visitante=visitante, estadio=local.estadio)
            if descansa:
                Descanso.objects.create(fecha=fecha, equipo=descansa, grupo=grupo)
            if ida_vuelta:
                fecha_vuelta = fechas[idx + total_rondas]
                for local, visitante in pares:
                    Partido.objects.create(campeonato=campeonato, fecha=fecha_vuelta, grupo=grupo,
                                           local=visitante, estadio=visitante.estadio,
                                           visitante=local, es_vuelta=True)
                if descansa:
                    Descanso.objects.create(fecha=fecha_vuelta, equipo=descansa, grupo=grupo)

    return _cerrar(fechas)
