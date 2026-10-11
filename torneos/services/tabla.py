"""Tabla de posiciones según reglas FIFA.

Puntos: victoria/empate/derrota configurables (3-1-0 por defecto). Empates de puntos se resuelven
con los criterios del campeonato, en orden. Sólo cuentan partidos FINALIZADOS.

Criterios soportados: dif_gol, goles_favor, h2h (puntos, dif. y goles entre los empatados),
fair_play (amarilla -1, doble amarilla -3, roja -4; mayor es mejor). Como último recurso,
orden alfabético (el "sorteo" queda a criterio del organizador).
"""
from collections import defaultdict

from torneos.models import EventoPartido, Inscripcion, Partido

FAIR_PLAY = {'AMARILLA': -1, 'DOBLE_AMARILLA': -3, 'ROJA': -4}
# (amarillas, rojas) que suma cada tipo de tarjeta: una doble amarilla cuenta 2 amarillas y 1 roja.
TARJETAS = {'AMARILLA': (1, 0), 'DOBLE_AMARILLA': (2, 1), 'ROJA': (0, 1)}


class Fila:
    def __init__(self, equipo, grupo=None):
        self.equipo = equipo
        self.grupo = grupo
        self.pj = self.pg = self.pe = self.pp = self.gf = self.gc = self.pts = 0
        self.amarillas = self.rojas = 0
        self.fair_play = 0
        self.forma = []  # últimos resultados: 'G', 'E', 'P'

    @property
    def dg(self):
        return self.gf - self.gc

    def sumar(self, gf, gc, campeonato):
        self.pj += 1
        self.gf += gf
        self.gc += gc
        if gf > gc:
            self.pg += 1
            self.pts += campeonato.puntos_victoria
            self.forma.append('G')
        elif gf == gc:
            self.pe += 1
            self.pts += campeonato.puntos_empate
            self.forma.append('E')
        else:
            self.pp += 1
            self.pts += campeonato.puntos_derrota
            self.forma.append('P')


def _partidos_validos(campeonato, grupo=None):
    qs = Partido.objects.filter(campeonato=campeonato, estado=Partido.Estado.FINALIZADO,
                                fecha__fase__in=['LIGA', 'GRUPOS'])
    if grupo is not None:
        qs = qs.filter(grupo=grupo)
    return list(qs.select_related('local', 'visitante', 'fecha').order_by('fecha__numero', 'fecha_hora', 'id'))


def _acumular(filas, partidos, campeonato):
    for p in partidos:
        if p.local_id in filas and p.visitante_id in filas:
            filas[p.local_id].sumar(p.goles_local, p.goles_visitante, campeonato)
            filas[p.visitante_id].sumar(p.goles_visitante, p.goles_local, campeonato)


def _tarjetas(filas, partidos):
    ids = [p.id for p in partidos]
    for ev in EventoPartido.objects.filter(partido_id__in=ids, tipo__in=FAIR_PLAY.keys()):
        fila = filas.get(ev.equipo_id)
        if not fila:
            continue
        fila.fair_play += FAIR_PLAY[ev.tipo]
        amarillas, rojas = TARJETAS[ev.tipo]
        fila.amarillas += amarillas
        fila.rojas += rojas


def _h2h(empatados, partidos, campeonato):
    """Mini-tabla sólo con los partidos entre los equipos empatados."""
    ids = {f.equipo.id for f in empatados}
    mini = {i: Fila(None) for i in ids}
    _acumular(mini, [p for p in partidos if p.local_id in ids and p.visitante_id in ids], campeonato)
    return {i: (m.pts, m.dg, m.gf) for i, m in mini.items()}


def _desempatar(filas, criterios, partidos, campeonato):
    """Ordena `filas` (con los mismos puntos) aplicando los criterios en secuencia."""
    if len(filas) <= 1:
        return list(filas)
    if not criterios:
        return sorted(filas, key=lambda f: f.equipo.nombre)
    criterio, resto = criterios[0], criterios[1:]
    if criterio == 'dif_gol':
        clave = lambda f: f.dg
    elif criterio == 'goles_favor':
        clave = lambda f: f.gf
    elif criterio == 'fair_play':
        clave = lambda f: f.fair_play
    elif criterio == 'h2h':
        mini = _h2h(filas, partidos, campeonato)
        clave = lambda f: mini[f.equipo.id]
    else:  # criterio desconocido: se ignora
        return _desempatar(filas, resto, partidos, campeonato)

    grupos = defaultdict(list)
    for f in filas:
        grupos[clave(f)].append(f)
    resultado = []
    for valor in sorted(grupos, reverse=True):
        resultado.extend(_desempatar(grupos[valor], resto, partidos, campeonato))
    return resultado


def calcular_tabla(campeonato, grupo=None):
    """Devuelve lista de dicts ordenada por posición. `grupo` limita a un grupo."""
    inscripciones = Inscripcion.objects.filter(campeonato=campeonato).select_related('equipo', 'grupo')
    if grupo is not None:
        inscripciones = inscripciones.filter(grupo=grupo)
    filas = {i.equipo_id: Fila(i.equipo, i.grupo) for i in inscripciones}
    partidos = _partidos_validos(campeonato, grupo)
    _acumular(filas, partidos, campeonato)
    _tarjetas(filas, partidos)

    por_puntos = defaultdict(list)
    for f in filas.values():
        por_puntos[f.pts].append(f)
    ordenadas = []
    for pts in sorted(por_puntos, reverse=True):
        ordenadas.extend(_desempatar(por_puntos[pts], list(campeonato.criterios_desempate), partidos, campeonato))

    return [{
        'posicion': pos,
        'equipo': f.equipo,
        'grupo': f.grupo.nombre if f.grupo else None,
        'pj': f.pj, 'pg': f.pg, 'pe': f.pe, 'pp': f.pp,
        'gf': f.gf, 'gc': f.gc, 'dg': f.dg, 'pts': f.pts,
        'amarillas': f.amarillas, 'rojas': f.rojas, 'fair_play': f.fair_play,
        'forma': f.forma[-5:],
    } for pos, f in enumerate(ordenadas, start=1)]


def tarjetas_jugadores(campeonato, limite=30):
    """Ranking disciplinario: jugadores con más tarjetas (más tarjetas totales, luego más rojas)."""
    acumulado, info = {}, {}
    eventos = EventoPartido.objects.filter(
        partido__campeonato=campeonato, tipo__in=TARJETAS.keys(), jugador__isnull=False,
    ).select_related('jugador', 'equipo')
    for ev in eventos:
        a, r = TARJETAS[ev.tipo]
        previo = acumulado.get(ev.jugador_id, (0, 0))
        acumulado[ev.jugador_id] = (previo[0] + a, previo[1] + r)
        info[ev.jugador_id] = ev
    orden = sorted(acumulado.items(),
                   key=lambda kv: (-(kv[1][0] + kv[1][1]), -kv[1][1], info[kv[0]].jugador.apellidos))[:limite]
    return [{'jugador': info[j].jugador, 'equipo': info[j].equipo, 'amarillas': a, 'rojas': r}
            for j, (a, r) in orden]


def tarjetas_por_equipo(eventos):
    """{'local'|'visitante': {amarillas, rojas}} para los eventos de un partido (ya cargados)."""
    totales = {}
    for ev in eventos:
        if ev.tipo in TARJETAS:
            a, r = TARJETAS[ev.tipo]
            previo = totales.get(ev.equipo_id, (0, 0))
            totales[ev.equipo_id] = (previo[0] + a, previo[1] + r)
    return totales


def goleadores(campeonato, limite=20):
    """Tabla de goleadores (no cuenta autogoles)."""
    conteo = defaultdict(int)
    eventos = EventoPartido.objects.filter(
        partido__campeonato=campeonato, tipo__in=['GOL', 'GOL_PENAL'], jugador__isnull=False,
    ).select_related('jugador', 'equipo')
    info = {}
    for ev in eventos:
        conteo[ev.jugador_id] += 1
        info[ev.jugador_id] = ev
    orden = sorted(conteo.items(), key=lambda kv: (-kv[1], info[kv[0]].jugador.apellidos))[:limite]
    return [{'jugador': info[j].jugador, 'equipo': info[j].equipo, 'goles': g} for j, g in orden]
