"""Asigna día y hora a los partidos según los días de la semana elegidos para el campeonato.

Ejemplo: se juega martes y jueves y cada fecha tiene 2 partidos el mismo día: la fecha 1 cae el martes,
la 2 el jueves, la 3 el martes siguiente... Con `partidos_por_dia` menor al total de la fecha, los partidos
restantes pasan al siguiente día de juego (la fecha ocupa varios días).
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from torneos.models import Partido
from torneos.services.feriados import CalendarioFeriados

HORA_POR_DEFECTO = time(19, 0)


class ProgramacionError(ValueError):
    pass


class ConflictoFeriados(Exception):
    """Algún partido caería en feriado y el usuario aún no decidió si se omite o se juega.

    `feriados`: lista ordenada de (date, nombre).
    """

    def __init__(self, feriados):
        super().__init__('Hay partidos que caen en feriado.')
        self.feriados = feriados

    def como_json(self):
        return [{'fecha': d.isoformat(), 'nombre': n} for d, n in self.feriados]


def validar_dias(dias):
    try:
        dias = sorted({int(d) for d in (dias or [])})
    except (TypeError, ValueError):
        raise ProgramacionError('Los días de la semana deben ser números del 1 (lunes) al 7 (domingo).')
    if any(d < 1 or d > 7 for d in dias):
        raise ProgramacionError('Los días de la semana deben estar entre 1 (lunes) y 7 (domingo).')
    return dias


def _siguiente_dia(desde, dias):
    """Primer día >= `desde` cuyo día de la semana está permitido."""
    dia = desde
    for _ in range(7):
        if dia.isoweekday() in dias:
            return dia
        dia += timedelta(days=1)
    return desde  # inalcanzable con dias no vacío


def _asignar_horas(partidos, dia, hora, minutos):
    inicio = datetime.combine(dia, hora)
    for i, partido in enumerate(partidos):
        partido.fecha_hora = timezone.make_aware(inicio + timedelta(minutes=minutos * i))


def _proximo(cursor, dias):
    """Primer día utilizable a partir de `cursor` (respetando los días de la semana, si hay)."""
    return _siguiente_dia(cursor, dias) if dias else cursor


def _planificar(campeonato, fechas, desde, feriados):
    """Calcula (sin guardar) el día de cada tanda de partidos.

    Devuelve (plan, conflictos, cursor): `plan` = [(fecha, [(dia, [partidos])])], `conflictos` = {dia: feriado}
    de los días que caen en feriado cuando `feriados is None`, y `cursor` el siguiente día libre.
    """
    dias = validar_dias(campeonato.dias_semana)
    paso = 1 if dias else max(campeonato.dias_entre_fechas, 1)
    calendario = CalendarioFeriados(campeonato)
    cursor = desde
    plan, conflictos = [], {}
    for fecha in fechas:
        partidos = list(fecha.partidos.order_by('id'))
        por_dia = campeonato.partidos_por_dia or max(len(partidos), 1)
        tandas = [partidos[i:i + por_dia] for i in range(0, len(partidos), por_dia)] or [[]]
        asignadas = []
        for tanda in tandas:
            dia = _proximo(cursor, dias)
            if feriados == 'omitir':
                for _ in range(800):  # tope de seguridad (más de dos años)
                    if not calendario.nombre(dia):
                        break
                    dia = _proximo(dia + timedelta(days=1), dias)
            elif feriados is None and calendario.nombre(dia):
                conflictos[dia] = calendario.nombre(dia)
            asignadas.append((dia, tanda))
            cursor = dia + timedelta(days=paso)
        plan.append((fecha, asignadas))
    return plan, conflictos, cursor


def programar(campeonato, fechas, desde, feriados=None):
    """Programa `fechas` (en orden) a partir del día `desde`. Devuelve la fecha siguiente libre.

    Usa la configuración del campeonato: `dias_semana`, `partidos_por_dia`, `hora_inicio`,
    `minutos_entre_partidos` y, sin días de la semana, `dias_entre_fechas` (una fecha por intervalo).

    Feriados: con `feriados='omitir'` los días feriados se saltan; con `'jugar'` se juega igual; con
    `None` (sin decidir) si algún partido cayera en feriado se lanza `ConflictoFeriados` sin cambiar nada,
    para que la interfaz pregunte al usuario.
    """
    if feriados not in (None, 'omitir', 'jugar'):
        raise ProgramacionError("Use feriados='omitir' o 'jugar'.")
    hora = campeonato.hora_inicio or HORA_POR_DEFECTO
    minutos = campeonato.minutos_entre_partidos or 90
    plan, conflictos, cursor = _planificar(campeonato, fechas, desde, feriados)

    if conflictos:
        raise ConflictoFeriados(sorted(conflictos.items()))

    modificados = []
    for fecha, asignadas in plan:
        for dia, tanda in asignadas:
            _asignar_horas(tanda, dia, hora, minutos)
            modificados.extend(tanda)
        fecha.fecha_programada = asignadas[0][0]
        fecha.save(update_fields=['fecha_programada'])
    Partido.objects.bulk_update(modificados, ['fecha_hora'])
    return cursor


def _fecha_automatica(campeonato, fecha):
    """Fechas cuyo día salió del sorteo (fase regular y primera ronda del relámpago).

    Las rondas de eliminatoria posteriores las programó el administrador a mano y no se mueven solas.
    """
    if fecha.fase in ('LIGA', 'GRUPOS'):
        return True
    return campeonato.modalidad == campeonato.Modalidad.RELAMPAGO and fecha.numero == 1


def aplicar_feriados(campeonato, simular=True):
    """Mueve al siguiente día disponible los partidos pendientes que caen en feriado (campeonato ya sorteado).

    Desde la primera fecha afectada se vuelve a calcular el calendario omitiendo los feriados, de modo que
    las fechas siguientes se corren en cadena y no se superponen. Sólo se tocan fechas con todos sus partidos
    pendientes; cada partido conserva su hora. Con `simular=True` no se guarda nada.

    Devuelve (cambios, sin_mover): `cambios` = [{fecha, nombre, desde, hasta, feriado, partidos}] y
    `sin_mover` = partidos pendientes en feriado que no se pueden mover solos (rondas programadas a mano).
    """
    calendario = CalendarioFeriados(campeonato)
    fechas = list(campeonato.fechas.order_by('numero').prefetch_related('partidos'))

    def dia_de(partido):
        return timezone.localtime(partido.fecha_hora).date() if partido.fecha_hora else None

    def avisar(f, p, motivo):
        dia = dia_de(p)
        if dia and calendario.nombre(dia):
            sin_mover.append({'fecha': f.nombre or f'Fecha {f.numero}', 'dia': dia.isoformat(),
                              'feriado': calendario.nombre(dia), 'motivo': motivo,
                              'partido': f'{p.local.nombre} vs {p.visitante.nombre}'})

    candidatas, sin_mover = [], []
    for f in fechas:
        partidos = list(f.partidos.all())
        if not partidos:
            continue
        if any(p.estado != Partido.Estado.PENDIENTE for p in partidos):
            # la fecha ya tiene partidos jugados o postergados: no se mueve sola; se avisa de los pendientes en feriado
            for p in partidos:
                if p.estado == Partido.Estado.PENDIENTE:
                    avisar(f, p, 'La fecha tiene partidos jugados o postergados')
            continue
        if _fecha_automatica(campeonato, f):
            if any(p.fecha_hora for p in partidos):
                candidatas.append(f)
        else:
            for p in partidos:
                avisar(f, p, 'Ronda programada a mano')

    primera = next((f for f in candidatas
                    if any(dia_de(p) and calendario.nombre(dia_de(p)) for p in f.partidos.all())), None)
    if primera is None:
        return [], sin_mover

    resto = [f for f in candidatas if f.numero >= primera.numero]
    desde = min(dia_de(p) for p in primera.partidos.all() if p.fecha_hora)
    plan, _, _ = _planificar(campeonato, resto, desde, 'omitir')

    cambios, modificados = [], []
    for fecha, asignadas in plan:
        antes = [dia_de(p) for p in fecha.partidos.all() if p.fecha_hora]
        movidos = 0
        for dia, tanda in asignadas:
            for p in tanda:
                hora_original = timezone.localtime(p.fecha_hora).time() if p.fecha_hora \
                    else (campeonato.hora_inicio or HORA_POR_DEFECTO)
                nuevo = timezone.make_aware(datetime.combine(dia, hora_original))
                if nuevo != p.fecha_hora:
                    p.fecha_hora = nuevo
                    modificados.append(p)
                    movidos += 1
        nuevo_primer_dia = asignadas[0][0]
        if movidos:
            cambios.append({
                'fecha': fecha.id, 'nombre': fecha.nombre or f'Fecha {fecha.numero}',
                'desde': min(antes).isoformat() if antes else None, 'hasta': nuevo_primer_dia.isoformat(),
                'feriado': next((calendario.nombre(d) for d in antes if calendario.nombre(d)), None),
                'partidos': movidos,
            })
            fecha.fecha_programada = nuevo_primer_dia
            if not simular:
                fecha.save(update_fields=['fecha_programada'])
    if not simular:
        Partido.objects.bulk_update(modificados, ['fecha_hora'])
    return cambios, sin_mover


def aplicar_configuracion(campeonato, dias_semana=None, partidos_por_dia=None, hora_inicio=None,
                          minutos_entre_partidos=None, dias_entre_fechas=None, duracion_tiempo=None):
    """Guarda la configuración de programación en el campeonato (valida entradas).

    `None` conserva el valor actual; en `partidos_por_dia`, '' o 0 lo limpia (todos los de la fecha el mismo día).
    """
    if dias_semana is not None:
        campeonato.dias_semana = validar_dias(dias_semana)
    if partidos_por_dia is not None:
        if partidos_por_dia in ('', 0, '0'):
            campeonato.partidos_por_dia = None
        else:
            try:
                campeonato.partidos_por_dia = int(partidos_por_dia)
            except (TypeError, ValueError):
                raise ProgramacionError('Los partidos por día deben ser un número.')
            if campeonato.partidos_por_dia < 1:
                raise ProgramacionError('Los partidos por día deben ser al menos 1.')
    if hora_inicio:
        try:
            campeonato.hora_inicio = time.fromisoformat(str(hora_inicio)[:5])
        except ValueError:
            raise ProgramacionError('La hora debe tener el formato HH:MM.')
    if minutos_entre_partidos:
        campeonato.minutos_entre_partidos = max(int(minutos_entre_partidos), 1)
    if dias_entre_fechas:
        campeonato.dias_entre_fechas = max(int(dias_entre_fechas), 1)
    if duracion_tiempo:
        try:
            duracion = int(duracion_tiempo)
        except (TypeError, ValueError):
            raise ProgramacionError('La duración de cada tiempo debe ser un número de minutos.')
        if not 1 <= duracion <= 120:
            raise ProgramacionError('La duración de cada tiempo debe estar entre 1 y 120 minutos.')
        campeonato.duracion_tiempo = duracion
    campeonato.save(update_fields=['dias_semana', 'partidos_por_dia', 'hora_inicio',
                                   'minutos_entre_partidos', 'dias_entre_fechas', 'duracion_tiempo'])


def programar_inicial(campeonato, fechas, fecha_inicio, feriados=None):
    """Programa el fixture recién generado si se indicó una fecha de inicio."""
    if not fecha_inicio:
        return
    if not isinstance(fecha_inicio, date):
        raise ProgramacionError('Fecha de inicio inválida.')
    programar(campeonato, fechas, fecha_inicio, feriados)
