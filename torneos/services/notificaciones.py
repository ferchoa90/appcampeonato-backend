"""Avisos de partido: cambios de estado, goles, tarjetas, cambios e incidencias.

Cada aviso se registra (bandeja del administrador) y se envía por dos vías:
 - temas de Firebase (`campeonato_<id>`, `equipo_<id>`) para la app móvil sin sesión;
 - a los dispositivos de los aficionados con sesión, según sus intereses (navegador y app) — ver services.push.

El envío va en segundo plano para no retrasar el guardado del partido ni del evento. Sin credenciales de
Firebase (FCM_CREDENTIALS_FILE) sólo se registra y se avisa a los navegadores.
"""
import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from torneos.models import EventoPartido, Notificacion, Partido

log = logging.getLogger(__name__)
_app = None

MENSAJES = {
    Partido.Estado.INICIADO: '¡Comenzó el partido!',
    Partido.Estado.ENTRETIEMPO: 'Entretiempo',
    Partido.Estado.SEGUNDO_TIEMPO: 'Comenzó el segundo tiempo',
    Partido.Estado.FINALIZADO: 'Partido finalizado',
    Partido.Estado.POSTERGADO: 'Partido postergado',
    Partido.Estado.PENDIENTE: 'Partido pendiente',
}


def _firebase():
    global _app
    if _app is None and settings.FCM_CREDENTIALS_FILE:
        import firebase_admin
        from firebase_admin import credentials
        _app = firebase_admin.initialize_app(credentials.Certificate(settings.FCM_CREDENTIALS_FILE))
    return _app


def firebase_disponible():
    try:
        return _firebase() is not None
    except Exception:  # noqa: BLE001 - credenciales inválidas: se trata como no disponible
        log.exception('Firebase no disponible')
        return False


def _enviar_temas(partido, titulo, cuerpo, extra):
    """Push a los temas del campeonato y de ambos equipos (app sin sesión). Devuelve True si se envió."""
    if not firebase_disponible():
        log.info('FCM sin credenciales; aviso guardado: %s - %s', titulo, cuerpo)
        return False
    from firebase_admin import messaging
    topics = [f'campeonato_{partido.campeonato_id}', f'equipo_{partido.local_id}', f'equipo_{partido.visitante_id}']
    data = {'partido_id': str(partido.id), 'campeonato_id': str(partido.campeonato_id), **extra}
    # un mismo dispositivo puede seguir campeonato y equipo: una condición evita duplicados
    condicion = ' || '.join(f"'{t}' in topics" for t in topics)
    messaging.send(messaging.Message(
        condition=condicion, data=data, notification=messaging.Notification(title=titulo, body=cuerpo),
        android=messaging.AndroidConfig(priority='high')))
    return True


def _enviar(noti_id, partido_id, tipo, titulo, cuerpo, extra):
    from torneos.services import push
    estado = {'enviada': False, 'destinatarios': 0, 'error': ''}
    try:
        partido = Partido.objects.select_related('campeonato', 'local', 'visitante').get(pk=partido_id)
        try:
            estado['enviada'] = _enviar_temas(partido, titulo, cuerpo, extra)
        except Exception as exc:  # noqa: BLE001 - un fallo de Firebase no debe impedir el aviso a navegadores
            log.exception('Error enviando push a temas')
            estado['error'] = str(exc)[:255]
        estado['destinatarios'] = push.notificar_aficionados(partido, tipo, titulo, cuerpo)
        estado['enviada'] = estado['enviada'] or estado['destinatarios'] > 0
    except Exception as exc:  # noqa: BLE001
        log.exception('Error enviando notificaciones')
        estado['error'] = str(exc)[:255]
    finally:
        Notificacion.objects.filter(pk=noti_id).update(**estado)
        if threading.current_thread() is not threading.main_thread():
            connection.close()


def emitir(partido, tipo, titulo, cuerpo, extra=None):
    """Registra el aviso y lo envía (en segundo plano salvo en pruebas). Devuelve el registro."""
    extra = {'tipo': tipo, **(extra or {})}
    noti = Notificacion.objects.create(campeonato=partido.campeonato, partido=partido, tipo=tipo,
                                       titulo=titulo[:150], cuerpo=cuerpo[:255])
    args = (noti.pk, partido.pk, tipo, titulo, cuerpo, extra)
    if getattr(settings, 'PUSH_EN_SEGUNDO_PLANO', True):
        transaction.on_commit(lambda: threading.Thread(target=_enviar, args=args, daemon=True).start())
    else:
        _enviar(*args)
        noti.refresh_from_db()
    return noti


# ---------------------------------------------------------------- mensajes

def _marcador(partido):
    return f'{partido.local.nombre} {partido.goles_local} - {partido.goles_visitante} {partido.visitante.nombre}'


def notificar_cambio_estado(partido):
    titulo = f'{partido.local.nombre} vs {partido.visitante.nombre}'
    cuerpo = MENSAJES.get(partido.estado, partido.get_estado_display())
    if partido.estado in (Partido.Estado.INICIADO, Partido.Estado.ENTRETIEMPO, Partido.Estado.SEGUNDO_TIEMPO,
                          Partido.Estado.FINALIZADO):
        cuerpo += f' · {partido.goles_local} - {partido.goles_visitante}'
    elif partido.estado == Partido.Estado.POSTERGADO:
        cuerpo += f': {partido.motivo_postergacion[:120]}. '
        cuerpo += ('Nueva fecha: ' + timezone.localtime(partido.fecha_hora).strftime('%d/%m %H:%M')
                   if partido.fecha_hora else 'Nueva fecha por definir')
    return emitir(partido, 'ESTADO', titulo, cuerpo, {'estado': partido.estado})


def notificar_marcador(partido):
    """El marcador cambió sin un evento de gol (corrección manual)."""
    return emitir(partido, 'GOL', f'Marcador actualizado: {partido.local.nombre} vs {partido.visitante.nombre}',
                  _marcador(partido))


def notificar_evento(evento):
    """Aviso de un evento recién registrado (gol, tarjeta, cambio, incidencia)."""
    partido = evento.partido
    quien = evento.jugador.nombre_completo if evento.jugador else evento.equipo.nombre
    minuto = f" · {evento.minuto}'" if evento.minuto else ''
    T = EventoPartido.Tipo
    if evento.tipo in (T.GOL, T.GOL_PENAL, T.AUTOGOL):
        etiqueta = {T.GOL: '¡Gol!', T.GOL_PENAL: '¡Gol de penal!', T.AUTOGOL: 'Autogol'}[evento.tipo]
        return emitir(partido, 'GOL', f'⚽ {etiqueta} {evento.equipo.nombre}', f'{quien}{minuto} — {_marcador(partido)}')
    if evento.tipo in (T.AMARILLA, T.DOBLE_AMARILLA, T.ROJA):
        etiqueta = {T.AMARILLA: '🟨 Tarjeta amarilla', T.DOBLE_AMARILLA: '🟨🟥 Doble amarilla', T.ROJA: '🟥 Tarjeta roja'}[evento.tipo]
        return emitir(partido, 'TARJETA', etiqueta, f'{quien} ({evento.equipo.nombre}){minuto} — {partido.local.nombre} vs {partido.visitante.nombre}')
    if evento.tipo == T.CAMBIO:
        entra = evento.jugador_relevo.nombre_completo if evento.jugador_relevo else '—'
        return emitir(partido, 'CAMBIO', f'🔁 Cambio en {evento.equipo.nombre}',
                      f'Sale {quien}, entra {entra}{minuto} — {partido.local.nombre} vs {partido.visitante.nombre}')
    return emitir(partido, 'INCIDENCIA', f'ℹ️ Incidencia: {partido.local.nombre} vs {partido.visitante.nombre}',
                  (evento.descripcion or 'Hay una novedad en el partido') + minuto)
