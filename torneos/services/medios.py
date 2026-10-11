"""Validación de fotos y videos de un partido (tipo real del archivo, cantidad y peso según Configuración)."""
import os

from PIL import Image, UnidentifiedImageError

from torneos.models import PartidoMedio

IMAGENES = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEOS = {'.mp4', '.m4v', '.mov', '.webm'}


class MedioError(ValueError):
    pass


def _es_video_real(archivo):
    """Comprueba la firma del contenedor (MP4/MOV: 'ftyp'; WebM: cabecera EBML), no sólo la extensión."""
    archivo.seek(0)
    cabecera = archivo.read(16)
    archivo.seek(0)
    return cabecera[4:8] == b'ftyp' or cabecera[:4] == b'\x1a\x45\xdf\xa3'


def _es_imagen_real(archivo):
    try:
        archivo.seek(0)
        with Image.open(archivo) as imagen:
            imagen.verify()
        return True
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return False
    finally:
        archivo.seek(0)


def validar(archivo, partido, config):
    """Devuelve el tipo (IMAGEN | VIDEO) o lanza MedioError con un mensaje claro."""
    extension = os.path.splitext(archivo.name)[1].lower()
    if extension in IMAGENES:
        tipo, maximo_mb, maximo_n = PartidoMedio.Tipo.IMAGEN, config.max_mb_imagen, config.max_imagenes_partido
        if not _es_imagen_real(archivo):
            raise MedioError('El archivo no es una imagen válida.')
    elif extension in VIDEOS:
        tipo, maximo_mb, maximo_n = PartidoMedio.Tipo.VIDEO, config.max_mb_video, config.max_videos_partido
        if not _es_video_real(archivo):
            raise MedioError('El archivo no es un video válido (use MP4, MOV o WebM).')
    else:
        raise MedioError('Formato no permitido. Imágenes: JPG, PNG, WebP o GIF. Videos: MP4, MOV o WebM.')

    plural = {PartidoMedio.Tipo.IMAGEN: 'imágenes', PartidoMedio.Tipo.VIDEO: 'videos'}[tipo]
    if archivo.size > maximo_mb * 1024 * 1024:
        raise MedioError(f'El archivo pesa {archivo.size / 1048576:.1f} MB y el máximo para {plural} es {maximo_mb} MB.')
    if partido.medios.filter(tipo=tipo).count() >= maximo_n:
        raise MedioError(f'Este partido ya tiene el máximo de {plural} permitido ({maximo_n}).')
    return tipo
