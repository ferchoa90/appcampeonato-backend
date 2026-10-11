"""Cifrado simétrico para secretos guardados en la base (clave SMTP, clave privada VAPID).

La clave se deriva de SECRET_KEY: si ésta cambia, los secretos guardados dejan de poder leerse y hay que
volver a configurarlos (se devuelve vacío, nunca se rompe la aplicación).
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PREFIJO = 'enc:'


def _fernet():
    clave = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(clave)


def cifrar(texto):
    if not texto:
        return ''
    return PREFIJO + _fernet().encrypt(texto.encode()).decode()


def descifrar(valor):
    if not valor:
        return ''
    if not valor.startswith(PREFIJO):
        return valor  # guardado en claro (p. ej. migrado a mano)
    try:
        return _fernet().decrypt(valor[len(PREFIJO):].encode()).decode()
    except InvalidToken:
        return ''
