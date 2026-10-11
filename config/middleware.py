from django.conf import settings


class DominioPublicoMiddleware:
    """Fija el dominio público del API cuando hay proxies intermedios (Nginx -> Apache -> Gunicorn) que no
    conservan el Host original. Así las URLs absolutas (logos, escudos, fotos) salen con el dominio correcto.
    Se activa sólo si PUBLIC_API_HOST está definido en el .env."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = getattr(settings, 'PUBLIC_API_HOST', '')
        if host:
            request.META['HTTP_HOST'] = host
            request.META.pop('HTTP_X_FORWARDED_HOST', None)
        return self.get_response(request)
