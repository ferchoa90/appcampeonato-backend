"""Envío de correos con la cuenta SMTP configurada en «Configuración» (y su prueba)."""
import logging
import smtplib
import socket
import ssl

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils.html import escape

from torneos.models import ConfiguracionSistema
from torneos.services.cifrado import descifrar

log = logging.getLogger(__name__)

SAL_VERIFICAR = 'goalscore.verificar-correo'
VIGENCIA_VERIFICACION = 60 * 60 * 48  # 48 horas


class CorreoError(Exception):
    """Error al enviar, con un mensaje claro en español."""


def configurado(config=None):
    config = config or ConfiguracionSistema.obtener()
    return bool(config.smtp_host and config.correo_remitente)


def _conexion(config):
    return get_connection(
        backend=getattr(settings, 'CORREO_BACKEND', 'django.core.mail.backends.smtp.EmailBackend'),
        host=config.smtp_host, port=config.smtp_puerto,
        username=config.smtp_usuario or None, password=descifrar(config.smtp_clave) or None,
        use_tls=config.smtp_seguridad == ConfiguracionSistema.Seguridad.STARTTLS,
        use_ssl=config.smtp_seguridad == ConfiguracionSistema.Seguridad.SSL,
        timeout=15, fail_silently=False,
    )


def _explicar(error):
    """Convierte el error técnico en una frase útil para quien configura la cuenta."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'El servidor rechazó el usuario o la contraseña. Si usa Gmail/Outlook, genere una «contraseña de aplicación».'
    if isinstance(error, smtplib.SMTPSenderRefused):
        return 'El servidor no acepta ese remitente. Use un correo remitente de su mismo dominio o cuenta.'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'El servidor rechazó el correo de destino.'
    if isinstance(error, (smtplib.SMTPConnectError, ConnectionRefusedError)):
        return 'No se pudo conectar: revise el servidor y el puerto.'
    if isinstance(error, (socket.timeout, TimeoutError)):
        return 'El servidor tardó demasiado en responder: revise el servidor, el puerto y la seguridad.'
    if isinstance(error, socket.gaierror):
        return 'No se encontró el servidor: revise el nombre del servidor SMTP.'
    if isinstance(error, (ssl.SSLError, smtplib.SMTPServerDisconnected)):
        return 'Falló la conexión segura: pruebe otra opción de seguridad (STARTTLS con 587, SSL/TLS con 465).'
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return 'El servidor no admite esa seguridad o autenticación: pruebe otra opción de seguridad.'
    return f'No se pudo enviar el correo: {error}'


def enviar_correo(asunto, texto, destinatarios, html=None):
    """Envía un correo con la configuración guardada. Lanza CorreoError (mensaje en español) si falla."""
    config = ConfiguracionSistema.obtener()
    if not configurado(config):
        raise CorreoError('El correo no está configurado: indique el servidor SMTP y el remitente en Configuración.')
    if isinstance(destinatarios, str):
        destinatarios = [destinatarios]
    try:
        mensaje = EmailMultiAlternatives(asunto, texto, config.correo_remitente, destinatarios, connection=_conexion(config))
        if html:
            mensaje.attach_alternative(html, 'text/html')
        mensaje.send()
    except Exception as e:  # noqa: BLE001 - se traduce a un mensaje entendible
        log.warning('Error enviando correo: %s', e)
        raise CorreoError(_explicar(e)) from e


def _plantilla(titulo, parrafos, boton=None):
    """HTML sencillo y compatible con clientes de correo."""
    cuerpo = ''.join(f'<p style="margin:0 0 14px;color:#344054;font-size:15px;line-height:1.5">{p}</p>' for p in parrafos)
    accion = (f'<p style="margin:24px 0"><a href="{escape(boton[1])}" style="background:#1d7a3b;color:#fff;text-decoration:none;'
              f'padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block">{escape(boton[0])}</a></p>') if boton else ''
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px">'
            f'<h2 style="color:#145a2c;margin:0 0 16px">{escape(titulo)}</h2>{cuerpo}{accion}'
            f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px">'
            f'<p style="color:#98a2b3;font-size:12px;margin:0">GoalScore · AcepSistemas todos los derechos reservados.</p></div>')


def enviar_prueba(destino):
    """Correo de prueba de la configuración. Devuelve el mensaje de éxito; lanza CorreoError si falla."""
    config = ConfiguracionSistema.obtener()
    enviar_correo(
        'Prueba de correo de GoalScore',
        'Si recibe este mensaje, la configuración de correo de GoalScore funciona correctamente.',
        destino,
        _plantilla('Prueba de correo', ['Si recibe este mensaje, la configuración de correo de <b>GoalScore</b> funciona correctamente.',
                                        f'Servidor: {escape(config.smtp_host)}:{config.smtp_puerto}']),
    )
    return f'Correo de prueba enviado a {destino}.'


def _base_url(config):
    return (config.url_publica or 'http://localhost:4200').rstrip('/')


def token_verificacion(user):
    return signing.dumps({'u': user.pk}, salt=SAL_VERIFICAR)


def leer_token_verificacion(token):
    """Devuelve el id de usuario del token o None si es inválido o venció."""
    try:
        return signing.loads(token, salt=SAL_VERIFICAR, max_age=VIGENCIA_VERIFICACION)['u']
    except (signing.BadSignature, KeyError):
        return None


def enviar_verificacion(user):
    config = ConfiguracionSistema.obtener()
    enlace = f'{_base_url(config)}/cuenta/verificar?token={token_verificacion(user)}'
    nombre = user.first_name or 'aficionado'
    enviar_correo(
        'Confirme su correo en GoalScore',
        f'Hola {nombre}: confirme su correo abriendo este enlace (vale 48 horas): {enlace}',
        user.email,
        _plantilla('Confirme su correo', [f'Hola {escape(nombre)}, gracias por registrarse en GoalScore.',
                                          'Confirme su correo para activar su cuenta. El enlace vale 48 horas.'],
                   ('Confirmar mi correo', enlace)),
    )


def enviar_restablecer(user, token):
    config = ConfiguracionSistema.obtener()
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    enlace = f'{_base_url(config)}/cuenta/restablecer?uid={uid}&token={token}'
    enviar_correo(
        'Restablecer su contraseña de GoalScore',
        f'Para elegir una nueva contraseña abra este enlace: {enlace} . Si no lo solicitó, ignore este mensaje.',
        user.email,
        _plantilla('Restablecer contraseña', ['Recibimos una solicitud para cambiar su contraseña.',
                                               'Si no fue usted, ignore este mensaje: su contraseña no cambiará.'],
                   ('Elegir nueva contraseña', enlace)),
    )
