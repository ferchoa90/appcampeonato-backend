"""Configuración de Django para GoalScore."""
import os
import sys
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'si')


def env_lista(name, default=''):
    return [x.strip() for x in os.getenv(name, default).split(',') if x.strip()]


# Seguro por defecto: DEBUG se enciende sólo en desarrollo (backend/.env con DEBUG=True).
DEBUG = env_bool('DEBUG', False)

SECRET_KEY = os.getenv('SECRET_KEY', '')
if not DEBUG and (len(SECRET_KEY) < 40 or SECRET_KEY.startswith('dev-')):
    raise ImproperlyConfigured(
        'Defina SECRET_KEY en el .env de producción (al menos 40 caracteres aleatorios). '
        'Genere una con: python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"')
SECRET_KEY = SECRET_KEY or 'dev-inseguro-solo-para-desarrollo'

# En producción hay que indicar los dominios del API; en desarrollo se acepta cualquiera.
ALLOWED_HOSTS = env_lista('ALLOWED_HOSTS', '*' if DEBUG else 'localhost,127.0.0.1')
CSRF_TRUSTED_ORIGINS = env_lista('CSRF_TRUSTED_ORIGINS')   # p. ej. https://api.goalscore.acepsistemas.com

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'torneos',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Base de datos: PostgreSQL por defecto. DB_ENGINE=sqlite solo para pruebas locales rápidas.
if os.getenv('DB_ENGINE', 'postgres').lower() == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', 'campeonato'),
            'USER': os.getenv('DB_USER', 'postgres'),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('DB_PORT', '5432'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'es'
TIME_ZONE = os.getenv('TIME_ZONE', 'America/Guayaquil')
USE_I18N = True
USE_TZ = True

# Archivos: en producción los sirve Nginx desde estas carpetas (fuera del código, para no perderlos al actualizar).
STATIC_URL = '/static/'
STATIC_ROOT = Path(os.getenv('STATIC_ROOT', BASE_DIR / 'staticfiles'))
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', BASE_DIR / 'media'))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Detrás de Nginx con HTTPS (producción): Django debe saber que la conexión original es segura para generar
# enlaces https (imágenes, correos) y marcar las cookies como seguras. Nginx envía X-Forwarded-Proto.
USAR_HTTPS = env_bool('USAR_HTTPS', not DEBUG)
if USAR_HTTPS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Si el proxy (Apache) no conserva el Host público, Django lo toma de X-Forwarded-Host.
    USE_X_FORWARDED_HOST = env_bool('USE_X_FORWARDED_HOST', False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('HSTS_SEGUNDOS', '31536000'))   # 0 para desactivar mientras se prueba

# Contador de intentos (límites por IP) compartido por todos los procesos de Gunicorn.
CACHES = {'default': {
    'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
    'LOCATION': os.getenv('CACHE_DIR', str(BASE_DIR / 'cache')),
}} if not DEBUG else {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    # Lectura pública, escritura solo para personal autorizado (ver torneos.permissions).
    'DEFAULT_PERMISSION_CLASSES': ('torneos.permissions.LecturaPublicaEscrituraStaff',),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    # el auto-registro de organizadores se limita por IP para evitar abusos
    'DEFAULT_THROTTLE_RATES': {'registro': os.getenv('REGISTRO_LIMITE', '10/hour'),
                               'publico': os.getenv('CUENTAS_LIMITE', '60/hour')},
    # Detrás de Nginx la IP real viene en X-Forwarded-For (1 proxy). Sin esto todos los visitantes compartirían
    # el mismo límite de intentos.
    **({'NUM_PROXIES': int(os.getenv('NUM_PROXIES', '1'))} if USAR_HTTPS else {}),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': False,
}

CORS_ALLOW_ALL_ORIGINS = env_bool('CORS_ALLOW_ALL', DEBUG)
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv('CORS_ORIGINS', '').split(',') if o.strip()]

# Notificaciones push (Firebase Cloud Messaging). Sin credenciales solo se registra en el log.
FCM_CREDENTIALS_FILE = os.getenv('FCM_CREDENTIALS_FILE', '')

# Los avisos se envían en segundo plano para no retrasar el guardado; en las pruebas se envían en el acto.
PUSH_EN_SEGUNDO_PLANO = 'test' not in sys.argv
# Backend de correo (SMTP real; las pruebas lo cambian). Los datos del servidor se guardan en «Configuración».
CORREO_BACKEND = os.getenv('CORREO_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')

# Los registros van a la salida estándar: en producción los recoge systemd (journalctl -u goalscore).
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'torneos': {'handlers': ['console'], 'level': 'INFO'},
        'django.request': {'handlers': ['console'], 'level': 'WARNING'},
    },
}
