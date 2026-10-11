"""Notificaciones push a aficionados: navegador (Web Push con claves VAPID) y app móvil (token FCM).

El aficionado elige sus intereses (todos los partidos o una selección de campeonatos y equipos, y los tipos de
movimiento). Aquí se calcula quién debe recibir cada aviso y se envía a sus dispositivos.
"""
import base64
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db.models import Q

from torneos.models import ConfiguracionSistema, Dispositivo, PreferenciasNotificacion
from torneos.services.cifrado import cifrar, descifrar

log = logging.getLogger(__name__)
User = get_user_model()


# ---------------------------------------------------------------- claves VAPID (Web Push)

def _b64url(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b'=').decode()


def generar_vapid():
    """Devuelve (publica, privada) en base64url: formatos que usan el navegador y pywebpush."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid01
    vapid = Vapid01()
    vapid.generate_keys()
    publica = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    privada = vapid.private_key.private_numbers().private_value.to_bytes(32, 'big')
    return _b64url(publica), _b64url(privada)


def asegurar_vapid(config=None, regenerar=False):
    """Crea las claves la primera vez (o las regenera: los navegadores deberán volver a activar el aviso)."""
    config = config or ConfiguracionSistema.obtener()
    if regenerar or not (config.vapid_publica and descifrar(config.vapid_privada)):
        publica, privada = generar_vapid()
        config.vapid_publica, config.vapid_privada = publica, cifrar(privada)
        config.save(update_fields=['vapid_publica', 'vapid_privada'])
        if regenerar:
            Dispositivo.objects.filter(tipo=Dispositivo.Tipo.WEB).delete()  # las suscripciones viejas ya no sirven
    return config


# ---------------------------------------------------------------- a quién se avisa

def usuarios_interesados(partido, tipo):
    """Usuarios activos cuyas preferencias piden este tipo de aviso para este partido (sólo campeonatos publicados)."""
    if not partido.campeonato.publicado:
        return []
    q = Q(modo=PreferenciasNotificacion.Modo.TODO) | (
        Q(modo=PreferenciasNotificacion.Modo.SELECCION)
        & (Q(campeonatos=partido.campeonato_id) | Q(equipos__in=[partido.local_id, partido.visitante_id])))
    prefs = (PreferenciasNotificacion.objects.filter(q, user__is_active=True).distinct().select_related('user'))
    return [p.user_id for p in prefs if tipo in (p.tipos or [])]


def dispositivos_para(partido, tipo):
    ids = usuarios_interesados(partido, tipo)
    return list(Dispositivo.objects.filter(user_id__in=ids, activo=True)) if ids else []


# ---------------------------------------------------------------- envío

OK, VENCIDA, ERROR = 'ok', 'vencida', 'error'


def _enviar_web(dispositivo, carga, config, privada):
    """Envía a un navegador. NO toca la base de datos (corre en hilos): devuelve OK, VENCIDA o ERROR."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(subscription_info=dispositivo.suscripcion, data=json.dumps(carga, ensure_ascii=False),
                vapid_private_key=privada, vapid_claims={'sub': config.vapid_contacto or 'mailto:soporte@goalscore.app'},
                ttl=3600, timeout=10)
        return OK
    except WebPushException as e:
        estado = getattr(getattr(e, 'response', None), 'status_code', None)
        if estado in (404, 410):  # el navegador canceló la suscripción
            return VENCIDA
        log.warning('Web Push falló (%s): %s', estado, e)
    except Exception as e:  # noqa: BLE001
        log.warning('Web Push error: %s', e)
    return ERROR


def _enviar_fcm(dispositivos, carga):
    from torneos.services.notificaciones import firebase_disponible
    if not firebase_disponible():
        return 0
    from firebase_admin import messaging
    enviados = 0
    for d in dispositivos:
        try:
            messaging.send(messaging.Message(
                token=d.token, data={k: str(v) for k, v in carga.items()},
                notification=messaging.Notification(title=carga['titulo'], body=carga['cuerpo']),
                android=messaging.AndroidConfig(priority='high')))
            enviados += 1
        except Exception as e:  # noqa: BLE001 - token vencido o inválido
            if 'registration-token' in str(e) or 'Requested entity was not found' in str(e):
                d.delete()
            else:
                log.warning('FCM falló: %s', e)
    return enviados


def enviar_a_dispositivos(dispositivos, carga):
    """Envía `carga` a los dispositivos. Devuelve cuántos recibieron el aviso."""
    if not dispositivos:
        return 0
    config = ConfiguracionSistema.obtener()
    web = [d for d in dispositivos if d.tipo == Dispositivo.Tipo.WEB and d.suscripcion]
    enviados = 0
    if web:
        config = asegurar_vapid(config)
        privada = descifrar(config.vapid_privada)
        with ThreadPoolExecutor(max_workers=8) as pool:
            resultados = list(pool.map(lambda d: _enviar_web(d, carga, config, privada), web))
        enviados += resultados.count(OK)
        vencidas = [d.pk for d, r in zip(web, resultados) if r == VENCIDA]
        if vencidas:  # se borran aquí, en el hilo principal (los hilos de envío no usan la base)
            Dispositivo.objects.filter(pk__in=vencidas).delete()
    enviados += _enviar_fcm([d for d in dispositivos if d.tipo == Dispositivo.Tipo.FCM], carga)
    return enviados


def carga_partido(partido, tipo, titulo, cuerpo):
    return {'titulo': titulo, 'cuerpo': cuerpo, 'tipo': tipo, 'partido_id': partido.pk,
            'campeonato_id': partido.campeonato_id, 'url': f'/partido/{partido.pk}', 'tag': f'partido-{partido.pk}'}


def notificar_aficionados(partido, tipo, titulo, cuerpo):
    """Avisa a los aficionados interesados. Devuelve cuántos dispositivos lo recibieron."""
    return enviar_a_dispositivos(dispositivos_para(partido, tipo), carga_partido(partido, tipo, titulo, cuerpo))
