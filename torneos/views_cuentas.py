"""Cuentas de aficionados, intereses y dispositivos de notificación, configuración del sistema y medios de partido."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from torneos.acceso import alcance_campeonatos
from torneos.models import (TIPOS_NOTIFICACION, ConfiguracionSistema, Dispositivo, PartidoMedio, PreferenciasNotificacion)
from torneos.permissions import ADMIN, AFICIONADO, OPERADOR, ORGANIZADOR, SoloAdministrador
from torneos.serializers import (ConfiguracionSerializer, DispositivoSerializer, MedioSerializer, PerfilSerializer,
                                 PreferenciasSerializer, RegistroAficionadoSerializer, UsuarioSerializer)
from torneos.services import correo, push

User = get_user_model()


class PublicoThrottle(AnonRateThrottle):
    """Límite por IP para las pantallas públicas de cuenta (registro, olvido de contraseña, Google)."""
    scope = 'publico'


def _error(detalle, codigo=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'detail': detalle, **extra}, status=codigo)


def _preferencias_de(user):
    return PreferenciasNotificacion.objects.get_or_create(
        user=user, defaults={'tipos': list(TIPOS_NOTIFICACION)})[0]


def _nuevo_aficionado(**datos):
    user = User.objects.create_user(**datos)
    user.groups.set([Group.objects.get_or_create(name=AFICIONADO)[0]])
    _preferencias_de(user)
    return user


# ---------------------------------------------------------------- registro y acceso de aficionados

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def registro_aficionado(request):
    """Registro con correo. Si el administrador exige verificar el correo, la cuenta queda inactiva hasta confirmarlo."""
    serializer = RegistroAficionadoSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    d = serializer.validated_data
    exigir = ConfiguracionSistema.obtener().verificar_correo and correo.configurado()
    with transaction.atomic():
        user = _nuevo_aficionado(username=d['email'], email=d['email'], password=d['password'],
                                 first_name=d['first_name'], last_name=d.get('last_name', ''), is_active=not exigir)
        if exigir:
            try:
                correo.enviar_verificacion(user)
            except correo.CorreoError:
                transaction.set_rollback(True)
                return _error('No pudimos enviar el correo de confirmación. Inténtelo más tarde.', status.HTTP_503_SERVICE_UNAVAILABLE)
    detalle = ('Le enviamos un correo para confirmar su cuenta. Revise su bandeja (y el spam).' if exigir
               else 'Cuenta creada. Ya puede iniciar sesión.')
    return Response({'detail': detalle, 'requiere_verificacion': exigir}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def verificar_correo(request):
    """Confirma el correo con el enlace recibido y activa la cuenta."""
    user_id = correo.leer_token_verificacion(str(request.data.get('token', '')))
    user = User.objects.filter(pk=user_id).first() if user_id else None
    if not user:
        return _error('El enlace no es válido o ya venció. Solicite uno nuevo.')
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=['is_active'])
    return Response({'detail': 'Correo confirmado. Ya puede iniciar sesión.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def reenviar_verificacion(request):
    """Reenvía el correo de confirmación (la respuesta es siempre la misma para no revelar qué correos existen)."""
    user = User.objects.filter(email__iexact=str(request.data.get('email', '')).strip(), is_active=False,
                               groups__name=AFICIONADO).first()
    if user and correo.configurado():
        try:
            correo.enviar_verificacion(user)
        except correo.CorreoError:
            pass
    return Response({'detail': 'Si el correo está pendiente de confirmar, le enviamos un nuevo enlace.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def google_login(request):
    """Inicio de sesión / registro con una cuenta de Google (credencial ID de Google Identity Services)."""
    from google.auth.transport import requests as transporte
    from google.oauth2 import id_token
    config = ConfiguracionSistema.obtener()
    if not config.google_client_id:
        return _error('El acceso con Google no está configurado.', status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        info = id_token.verify_oauth2_token(str(request.data.get('credential', '')), transporte.Request(), config.google_client_id)
    except ValueError:
        return _error('No se pudo validar la cuenta de Google.', status.HTTP_401_UNAUTHORIZED)
    if not info.get('email_verified') or not info.get('email'):
        return _error('Su correo de Google no está verificado.', status.HTTP_401_UNAUTHORIZED)
    email = info['email'].strip().lower()
    user = User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email)).first()
    if user is None:
        user = _nuevo_aficionado(username=email, email=email, first_name=info.get('given_name') or info.get('name', '')[:150],
                                 last_name=info.get('family_name', '')[:150])
        user.set_unusable_password()
        user.save(update_fields=['password'])
    elif not user.groups.filter(name=AFICIONADO).exists():
        return _error('Esa cuenta se usa con usuario y contraseña.', status.HTTP_403_FORBIDDEN)
    elif not user.is_active:
        user.is_active = True  # Google ya verificó el correo
        user.save(update_fields=['is_active'])
    refresco = RefreshToken.for_user(user)
    return Response({'access': str(refresco.access_token), 'refresh': str(refresco), 'usuario': UsuarioSerializer(user).data})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def olvide_clave(request):
    """Envía el enlace para elegir una contraseña nueva (respuesta siempre igual)."""
    user = User.objects.filter(email__iexact=str(request.data.get('email', '')).strip(), is_active=True).first()
    if user and user.has_usable_password() and correo.configurado():
        try:
            correo.enviar_restablecer(user, default_token_generator.make_token(user))
        except correo.CorreoError:
            pass
    return Response({'detail': 'Si el correo existe, recibirá un enlace para cambiar su contraseña.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PublicoThrottle])
def restablecer_clave(request):
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(str(request.data.get('uid', '')))))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    if not user or not default_token_generator.check_token(user, str(request.data.get('token', ''))):
        return _error('El enlace no es válido o ya venció. Solicite uno nuevo.')
    try:
        validate_password(request.data.get('password', ''), user)
    except DjangoValidationError as e:
        return _error(' '.join(e.messages), password=list(e.messages))
    user.set_password(request.data['password'])
    user.save(update_fields=['password'])
    return Response({'detail': 'Contraseña actualizada. Ya puede iniciar sesión.'})


@api_view(['GET'])
@permission_classes([AllowAny])
def configuracion_publica(request):
    """Datos que necesita el portal: cliente de Google, clave pública de notificaciones y límites de medios."""
    config = push.asegurar_vapid()
    return Response({
        'google_client_id': config.google_client_id,
        'verificar_correo': config.verificar_correo and correo.configurado(config),
        'vapid_publica': config.vapid_publica,
        'limites': {'max_imagenes_partido': config.max_imagenes_partido, 'max_videos_partido': config.max_videos_partido,
                    'max_mb_imagen': config.max_mb_imagen, 'max_mb_video': config.max_mb_video},
        'tipos_notificacion': TIPOS_NOTIFICACION,
    })


# ---------------------------------------------------------------- mi cuenta, intereses y dispositivos

@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def mi_perfil(request):
    if request.method == 'PATCH':
        serializer = PerfilSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(PerfilSerializer(request.user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cambiar_clave(request):
    user = request.user
    if user.has_usable_password() and not user.check_password(request.data.get('actual', '')):
        return _error('La contraseña actual no es correcta.')
    try:
        validate_password(request.data.get('nueva', ''), user)
    except DjangoValidationError as e:
        return _error(' '.join(e.messages))
    user.set_password(request.data['nueva'])
    user.save(update_fields=['password'])
    return Response({'detail': 'Contraseña actualizada.'})


@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def mis_preferencias(request):
    """Intereses de notificación: modo (NADA | TODO | SELECCION), tipos de aviso, campeonatos y equipos."""
    prefs = _preferencias_de(request.user)
    if request.method != 'GET':
        serializer = PreferenciasSerializer(prefs, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(PreferenciasSerializer(prefs, context={'request': request}).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def mis_dispositivos(request):
    if request.method == 'POST':
        serializer = DispositivoSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        dispositivo = serializer.save()
        return Response(DispositivoSerializer(dispositivo).data, status=status.HTTP_201_CREATED)
    return Response(DispositivoSerializer(request.user.dispositivos.all(), many=True).data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def mi_dispositivo(request, pk):
    eliminados, _ = Dispositivo.objects.filter(pk=pk, user=request.user).delete()
    return Response(status=status.HTTP_204_NO_CONTENT if eliminados else status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def probar_notificacion(request):
    """Envía un aviso de prueba a los dispositivos del usuario."""
    dispositivos = list(request.user.dispositivos.filter(activo=True))
    if not dispositivos:
        return _error('Aún no tiene dispositivos registrados: active las notificaciones primero.')
    enviados = push.enviar_a_dispositivos(dispositivos, {
        'titulo': 'GoalScore', 'cuerpo': 'Las notificaciones funcionan. Recibirá los avisos de sus partidos.',
        'tipo': 'PRUEBA', 'url': '/perfil', 'tag': 'prueba'})
    return Response({'enviados': enviados, 'detail': f'Aviso de prueba enviado a {enviados} de {len(dispositivos)} dispositivo(s).'})


# ---------------------------------------------------------------- configuración (sólo Administrador)

class ConfiguracionView(APIView):
    permission_classes = [SoloAdministrador]

    def get(self, request):
        config = push.asegurar_vapid()
        return Response(ConfiguracionSerializer(config).data)

    def put(self, request):
        config = ConfiguracionSistema.obtener()
        serializer = ConfiguracionSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    patch = put


@api_view(['POST'])
@permission_classes([SoloAdministrador])
def probar_correo(request):
    """Envía un correo de prueba con la configuración guardada."""
    destino = str(request.data.get('destino', '')).strip()
    if '@' not in destino:
        return _error('Indique un correo de destino válido.')
    try:
        return Response({'ok': True, 'detail': correo.enviar_prueba(destino)})
    except correo.CorreoError as e:
        return Response({'ok': False, 'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([SoloAdministrador])
def regenerar_vapid(request):
    """Genera nuevas claves de notificaciones web. Los navegadores ya suscritos deberán volver a activarlas."""
    config = push.asegurar_vapid(regenerar=True)
    return Response({'vapid_publica': config.vapid_publica, 'detail': 'Claves regeneradas. Los navegadores deben volver a activar los avisos.'})


# ---------------------------------------------------------------- fotos y videos de un partido

class MedioViewSet(viewsets.ModelViewSet):
    """Lectura pública (campeonatos publicados). Suben y eliminan el Administrador, el organizador del campeonato
    y el Operador (desde el campo, con la app) de los campeonatos que puede operar."""
    queryset = PartidoMedio.objects.select_related('partido')
    serializer_class = MedioSerializer
    roles_escritura = (ADMIN, ORGANIZADOR, OPERADOR)
    filterset_fields = ['partido', 'tipo']
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return super().get_queryset().filter(partido__campeonato__in=alcance_campeonatos(self.request.user).values('pk'))
