from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from torneos import views, views_cuentas as cuentas

router = DefaultRouter()
router.register('equipos', views.EquipoViewSet)
router.register('jugadores', views.JugadorViewSet)
router.register('campeonatos', views.CampeonatoViewSet)
router.register('fechas', views.FechaViewSet)
router.register('feriados', views.FeriadoViewSet)
router.register('partidos', views.PartidoViewSet)
router.register('eventos', views.EventoViewSet)
router.register('notificaciones', views.NotificacionViewSet)
router.register('usuarios', views.UsuarioViewSet)
router.register('medios', cuentas.MedioViewSet)

urlpatterns = [
    path('auth/login', views.LoginView.as_view()),
    path('auth/token/refresh', TokenRefreshView.as_view()),
    path('auth/me', views.me),
    path('auth/registro', views.registro),
    path('auth/registro-aficionado', cuentas.registro_aficionado),
    path('auth/verificar-correo', cuentas.verificar_correo),
    path('auth/reenviar-verificacion', cuentas.reenviar_verificacion),
    path('auth/google', cuentas.google_login),
    path('auth/olvide-clave', cuentas.olvide_clave),
    path('auth/restablecer-clave', cuentas.restablecer_clave),
    path('publico/configuracion', cuentas.configuracion_publica),
    path('mi/perfil', cuentas.mi_perfil),
    path('mi/cambiar-clave', cuentas.cambiar_clave),
    path('mi/preferencias', cuentas.mis_preferencias),
    path('mi/dispositivos', cuentas.mis_dispositivos),
    path('mi/dispositivos/<int:pk>', cuentas.mi_dispositivo),
    path('mi/probar-notificacion', cuentas.probar_notificacion),
    path('configuracion', cuentas.ConfiguracionView.as_view()),
    path('configuracion/probar-correo', cuentas.probar_correo),
    path('configuracion/regenerar-vapid', cuentas.regenerar_vapid),
    path('salud', views.salud),
    path('', include(router.urls)),
]
