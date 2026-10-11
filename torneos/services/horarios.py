"""Horarios fijos por equipo (logística): p. ej. los partidos de BODEGA siempre en el 2.º horario del día.

Sólo cambia el ORDEN de los partidos dentro de un mismo día: se intercambian las horas. No se tocan los
cruces, las fechas ni los días. Se ignoran los días con algún partido ya iniciado, jugado o postergado.
"""
from collections import defaultdict

from django.utils import timezone

from torneos.models import Partido


class HorariosError(ValueError):
    pass


def validar_reglas(campeonato, reglas):
    """Normaliza y valida `[{'equipo': id, 'orden': n}]`; el equipo debe estar inscrito y no repetirse."""
    if reglas is None:
        reglas = []
    if not isinstance(reglas, list):
        raise HorariosError('Las reglas deben ser una lista de {equipo, orden}.')
    inscritos = set(campeonato.inscripciones.values_list('equipo_id', flat=True))
    limpias, vistos = [], set()
    for r in reglas:
        try:
            equipo, orden = int(r['equipo']), int(r['orden'])
        except (KeyError, TypeError, ValueError):
            raise HorariosError('Cada regla necesita un equipo y el número de horario (1, 2, 3...).')
        if orden < 1 or orden > 10:
            raise HorariosError('El horario debe estar entre 1 y 10.')
        if equipo not in inscritos:
            raise HorariosError('Hay un equipo que no está inscrito en este campeonato.')
        if equipo in vistos:
            raise HorariosError('Un equipo no puede tener dos reglas.')
        vistos.add(equipo)
        limpias.append({'equipo': equipo, 'orden': orden})
    return limpias


def _hora(momento):
    return timezone.localtime(momento).strftime('%H:%M')


def aplicar_horarios_fijos(campeonato, reglas=None, simular=False):
    """Reordena las horas de los partidos pendientes según las reglas (por defecto, las guardadas).

    Devuelve (cambios, sin_aplicar): `cambios` = [{fecha, dia, partido, de, a}] y `sin_aplicar` = reglas que no
    se pudieron cumplir en algún día (el equipo juega solo ese día, o dos reglas piden el mismo horario).
    """
    reglas = campeonato.horarios_fijos if reglas is None else reglas
    if not reglas:
        return [], []
    partidos = list(campeonato.partidos.select_related('local', 'visitante', 'fecha')
                    .filter(fecha_hora__isnull=False).order_by('fecha_hora', 'id'))
    por_dia = defaultdict(list)
    for p in partidos:
        por_dia[(p.fecha_id, timezone.localtime(p.fecha_hora).date())].append(p)

    nombres = {}
    for p in partidos:
        nombres[p.local_id], nombres[p.visitante_id] = p.local.nombre, p.visitante.nombre

    cambios, sin_aplicar = [], []
    modificados = []
    for (fecha_id, dia), lista in sorted(por_dia.items(), key=lambda kv: (kv[1][0].fecha.numero, kv[0][1])):
        if any(p.estado != Partido.Estado.PENDIENTE for p in lista):
            continue
        horas = [p.fecha_hora for p in lista]            # ya vienen ordenadas
        orden = list(lista)
        for regla in reglas:
            equipo, k = regla['equipo'], regla['orden'] - 1
            propio = next((p for p in orden if equipo in (p.local_id, p.visitante_id)), None)
            if propio is None:
                continue
            i = orden.index(propio)
            if k >= len(orden):
                sin_aplicar.append({'equipo': nombres.get(equipo, ''), 'dia': dia.isoformat(), 'fecha': propio.fecha.nombre,
                                    'motivo': f'ese día solo hay {len(orden)} horario(s)'})
                continue
            if i == k:
                continue
            ocupante = orden[k]
            choque = next((r for r in reglas if r['equipo'] != equipo and r['orden'] - 1 == k
                           and r['equipo'] in (ocupante.local_id, ocupante.visitante_id)), None)
            if choque:
                sin_aplicar.append({'equipo': nombres.get(equipo, ''), 'dia': dia.isoformat(), 'fecha': propio.fecha.nombre,
                                    'motivo': f'{nombres.get(choque["equipo"], "otro equipo")} ya ocupa ese horario'})
                continue
            orden[i], orden[k] = ocupante, propio
        for p, hora in zip(orden, horas):
            if p.fecha_hora != hora:
                cambios.append({'fecha': p.fecha.nombre, 'dia': dia.isoformat(),
                                'partido': f'{p.local.nombre} vs {p.visitante.nombre}',
                                'de': _hora(p.fecha_hora), 'a': _hora(hora)})
                p.fecha_hora = hora
                modificados.append(p)
    if modificados and not simular:
        Partido.objects.bulk_update(modificados, ['fecha_hora'])
    return cambios, sin_aplicar
