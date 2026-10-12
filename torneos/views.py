from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes, throttle_classes
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from torneos.acceso import alcance_campeonatos, alcance_equipos, es_admin
from torneos.models import (Campeonato, Equipo, EventoPartido, Fecha, Feriado, Inscripcion, Jugador, Notificacion,
                            Partido)
from torneos.permissions import ADMIN, AFICIONADO, OPERADOR, ORGANIZADOR, AdministradorOOrganizador
from torneos.serializers import (CampeonatoSerializer, EquipoMiniSerializer, EquipoSerializer, EventoSerializer,
                                 FechaCalendarioSerializer, FechaSerializer, FeriadoSerializer, InscripcionSerializer,
                                 JugadorSerializer, NotificacionSerializer, PartidoDetalleSerializer,
                                 PartidoSerializer, RegistroSerializer, UsuarioSerializer)
from torneos.services import eliminatoria, fixture, horarios, pdf, programacion, tabla
from torneos.services.feriados import CalendarioFeriados

User = get_user_model()


# ---------------------------------------------------------------- autenticación

def _usuario_para_login(valor):
    """El teclado del celular pone mayúsculas y la app envía el texto en minúsculas: se acepta el usuario sin
    distinguirlas (si es inequívoco) y también el correo de la cuenta. Un usuario exacto siempre tiene prioridad."""
    valor = (valor or '').strip()
    if not valor or User.objects.filter(username=valor).exists():
        return valor
    por_usuario = list(User.objects.filter(username__iexact=valor).values_list('username', flat=True)[:2])
    if len(por_usuario) == 1:
        return por_usuario[0]
    if '@' in valor:
        por_correo = list(User.objects.filter(email__iexact=valor).values_list('username', flat=True)[:2])
        if len(por_correo) == 1:
            return por_correo[0]
    return valor


class LoginSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        attrs[self.username_field] = _usuario_para_login(attrs.get(self.username_field))
        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            # cuenta correcta pero todavía inactiva (auto-registro pendiente de aprobación): se dice claramente
            user = User.objects.filter(username=attrs.get(self.username_field)).first()
            if user and not user.is_active and user.check_password(attrs.get('password', '')):
                raise AuthenticationFailed('Su cuenta aún no está activa: un administrador debe aprobarla.',
                                           code='cuenta_inactiva')
            raise
        data['usuario'] = UsuarioSerializer(self.user).data
        return data


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer


class RegistroThrottle(AnonRateThrottle):
    scope = 'registro'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegistroThrottle])
def registro(request):
    """Auto-registro de organizadores: la cuenta queda inactiva hasta que un administrador la apruebe."""
    serializer = RegistroSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({'detail': 'Cuenta creada. Un administrador debe aprobarla antes de que pueda ingresar.'},
                    status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UsuarioSerializer(request.user).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def salud(request):
    return Response({'status': 'ok', 'hora': timezone.now()})


# ---------------------------------------------------------------- catálogos

class UsuarioViewSet(viewsets.ModelViewSet):
    """El Administrador gestiona a todos; el Organizador sólo a los operadores que él creó."""
    queryset = User.objects.all().order_by('username')
    serializer_class = UsuarioSerializer
    permission_classes = [AdministradorOOrganizador]
    search_fields = ['username', 'first_name', 'last_name', 'email']
    filterset_fields = ['is_active']

    def get_queryset(self):
        qs = super().get_queryset()
        if not es_admin(self.request.user):
            qs = qs.filter(perfil__creado_por=self.request.user)
        else:
            # los aficionados (muchos, sin acceso al panel) sólo se listan si se piden: ?rol=Aficionado
            rol = self.request.query_params.get('rol')
            qs = qs.filter(groups__name=rol) if rol else qs.exclude(groups__name=AFICIONADO)
        if self.request.query_params.get('pendientes'):
            qs = qs.filter(is_active=False, last_login__isnull=True, perfil__creado_por__isnull=True,
                           groups__name=ORGANIZADOR)
        return qs

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user == request.user:
            return _error('No puede eliminar su propio usuario.')
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return _error(f'No se puede eliminar a «{user.username}»: tiene campeonatos, equipos u operadores a su nombre. '
                          'Desactive su cuenta en su lugar.', status.HTTP_409_CONFLICT, puede_desactivar=True)


class EquipoViewSet(viewsets.ModelViewSet):
    queryset = Equipo.objects.all()
    serializer_class = EquipoSerializer
    roles_escritura = (ADMIN, ORGANIZADOR)
    search_fields = ['nombre', 'abreviatura', 'ciudad']
    filterset_fields = {'activo': ['exact'], 'propietario': ['exact', 'isnull']}

    def get_queryset(self):
        # administración: sus equipos; público: sólo los de campeonatos publicados
        qs = super().get_queryset().filter(pk__in=alcance_equipos(self.request.user).values('pk'))
        campeonato = self.request.query_params.get('campeonato')
        if campeonato and campeonato.isdigit():  # equipos inscritos en ese campeonato
            qs = qs.filter(pk__in=Inscripcion.objects.filter(campeonato_id=campeonato).values('equipo_id'))
        return qs

    def destroy(self, request, *args, **kwargs):
        """Un equipo con partidos no se puede eliminar (se perdería el historial): se explica y se ofrece desactivarlo."""
        equipo = self.get_object()
        partidos = Partido.objects.filter(Q(local=equipo) | Q(visitante=equipo))
        cantidad = partidos.count()
        if cantidad:
            campeonatos = sorted(set(partidos.values_list('campeonato__nombre', flat=True)))
            return _error(
                f'No se puede eliminar «{equipo.nombre}»: participa en {cantidad} partido(s) de {", ".join(campeonatos)}. '
                'Limpie el sorteo de esos campeonatos (si aún no comenzaron) o desactive el equipo.',
                status.HTTP_409_CONFLICT, puede_desactivar=True, partidos=cantidad, campeonatos=campeonatos)
        return super().destroy(request, *args, **kwargs)


class JugadorViewSet(viewsets.ModelViewSet):
    queryset = Jugador.objects.select_related('equipo')
    serializer_class = JugadorSerializer
    roles_escritura = (ADMIN, ORGANIZADOR)
    search_fields = ['nombres', 'apellidos', 'identificacion']
    filterset_fields = ['equipo', 'posicion', 'activo']

    def get_queryset(self):
        return super().get_queryset().filter(equipo__in=alcance_equipos(self.request.user).values('pk'))


# ---------------------------------------------------------------- campeonatos

def _error(detalle, codigo=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'detail': detalle, **extra}, status=codigo)


def _serializar_tabla(filas, request):
    return [{**f, 'equipo': EquipoMiniSerializer(f['equipo'], context={'request': request}).data} for f in filas]


class CampeonatoViewSet(viewsets.ModelViewSet):
    queryset = Campeonato.objects.select_related('propietario')
    serializer_class = CampeonatoSerializer
    roles_escritura = (ADMIN, ORGANIZADOR)
    search_fields = ['nombre', 'temporada']
    filterset_fields = {'estado': ['exact'], 'formato': ['exact'], 'publicado': ['exact'],
                        'propietario': ['exact', 'isnull']}

    def get_queryset(self):
        # administración: lo que gestiona (el Administrador, todo); público: sólo lo publicado
        return super().get_queryset().filter(pk__in=alcance_campeonatos(self.request.user).values('pk'))

    # --- equipos inscritos
    @action(detail=True, methods=['get', 'post'])
    def equipos(self, request, pk=None):
        camp = self.get_object()
        if request.method == 'POST':
            if camp.partidos.exists():
                return _error('No se pueden cambiar los equipos: el fixture ya fue generado.')
            ids = set(request.data.get('equipos', []))
            # sólo equipos del mismo dueño que el campeonato (los del organizador, o los del sistema si no tiene dueño)
            permitidos = set(Equipo.objects.filter(id__in=ids, propietario=camp.propietario).values_list('id', flat=True))
            if ids - permitidos:
                return _error(f'Equipos inexistentes o que no pertenecen a este campeonato: {sorted(ids - permitidos)}')
            existentes = permitidos
            camp.inscripciones.exclude(equipo_id__in=ids).delete()
            for eid in ids:
                Inscripcion.objects.get_or_create(campeonato=camp, equipo_id=eid)
        return self._inscripciones(camp, request)

    @staticmethod
    def _inscripciones(camp, request):
        qs = camp.inscripciones.select_related('equipo', 'grupo').order_by('grupo__nombre', 'equipo__nombre')
        return Response(InscripcionSerializer(qs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='sortear-grupos')
    def sortear_grupos(self, request, pk=None):
        camp = self.get_object()
        try:
            fixture.sortear_grupos(camp)
        except fixture.FixtureError as e:
            return _error(str(e))
        return self._inscripciones(camp, request)

    # --- fixture
    @action(detail=True, methods=['get'], url_path='fixture-info')
    def fixture_info(self, request, pk=None):
        """Permite a la interfaz saber si debe preguntar por el descanso antes de sortear."""
        camp = self.get_object()
        try:
            impares = fixture.grupos_impares(camp)
        except fixture.FixtureError as e:
            return _error(str(e))
        modalidades = [{'valor': v, 'nombre': n} for v, n in Campeonato.Modalidad.choices]
        if camp.formato != Campeonato.Formato.LIGA:  # el relámpago no usa grupos
            modalidades = [m for m in modalidades if m['valor'] != Campeonato.Modalidad.RELAMPAGO]
        return Response({
            'equipos': camp.inscripciones.count(),
            'fixture_generado': camp.partidos.exists(),
            'sorteo_bloqueado': fixture.sorteo_bloqueado(camp),
            'hay_descanso': bool(impares),
            'grupos_impares': [{'grupo': g, 'equipos': n} for g, n in impares],
            'modalidad_actual': camp.modalidad,
            'modalidades': modalidades,
            'programacion': {
                'dias_semana': camp.dias_semana,
                'partidos_por_dia': camp.partidos_por_dia,
                'hora_inicio': camp.hora_inicio.strftime('%H:%M') if camp.hora_inicio else None,
                'minutos_entre_partidos': camp.minutos_entre_partidos,
                'dias_entre_fechas': camp.dias_entre_fechas,
            },
        })

    @action(detail=True, methods=['post'], url_path='generar-fixture')
    def generar_fixture(self, request, pk=None):
        camp = self.get_object()
        d = request.data
        try:
            fechas = fixture.generar_fixture(
                camp,
                modalidad=d.get('modalidad'),
                ida_vuelta=d.get('ida_vuelta'),
                confirmar_descanso=bool(d.get('confirmar_descanso', False)),
                semilla=d.get('semilla'),
                fecha_inicio=_fecha(d.get('fecha_inicio')),
                dias_entre_fechas=d.get('dias_entre_fechas'),
                dias_semana=d.get('dias_semana'),
                partidos_por_dia=d.get('partidos_por_dia'),
                hora_inicio=d.get('hora_inicio'),
                minutos_entre_partidos=d.get('minutos_entre_partidos'),
                primera_fecha=d.get('primera_fecha'),
                feriados=d.get('feriados'),
                regenerar=bool(d.get('regenerar', False)),
            )
        except programacion.ConflictoFeriados as e:
            return _error('Hay partidos que caen en feriado. ¿Se omiten esas fechas o se juega igual?',
                          status.HTTP_409_CONFLICT, requiere_decision_feriados=True, feriados=e.como_json())
        except fixture.RequiereConfirmacion as e:
            return _error(e.detalle, status.HTTP_409_CONFLICT, requiere_confirmacion=True)
        except (fixture.FixtureError, ValueError) as e:
            return _error(str(e))
        return Response({'fechas': len(fechas), 'partidos': camp.partidos.count()}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='limpiar-sorteo')
    def limpiar_sorteo(self, request, pk=None):
        """Elimina todo el sorteo (fechas, partidos, descansos y grupos). Conserva los equipos inscritos."""
        try:
            resumen = fixture.limpiar_sorteo(self.get_object())
        except fixture.FixtureError as e:
            return _error(str(e))
        return Response(resumen)

    @action(detail=True, methods=['post'], url_path='horarios-fijos')
    def horarios_fijos(self, request, pk=None):
        """Reglas de logística: un equipo siempre en el N-ésimo horario del día (sólo reordena horas).

        `{reglas: [{equipo, orden}], confirmar?}`. Sin `confirmar` sólo simula; con `confirmar` guarda las reglas
        (se vuelven a aplicar si se repite el sorteo) y reordena los partidos pendientes.
        """
        camp = self.get_object()
        try:
            reglas = horarios.validar_reglas(camp, request.data.get('reglas'))
        except horarios.HorariosError as e:
            return _error(str(e))
        confirmar = bool(request.data.get('confirmar', False))
        with transaction.atomic():
            cambios, sin_aplicar = horarios.aplicar_horarios_fijos(camp, reglas, simular=not confirmar)
            if confirmar:
                camp.horarios_fijos = reglas
                camp.save(update_fields=['horarios_fijos'])
        return Response({'aplicado': confirmar, 'reglas': reglas, 'cambios': cambios, 'sin_aplicar': sin_aplicar})

    @action(detail=True, methods=['post'], url_path='aplicar-feriados')
    def aplicar_feriados(self, request, pk=None):
        """Mueve al siguiente día disponible los partidos pendientes que caen en feriado.

        Sin `confirmar` sólo simula y devuelve los cambios; con `confirmar: true` los aplica.
        """
        camp = self.get_object()
        confirmar = bool(request.data.get('confirmar', False))
        cambios, sin_mover = programacion.aplicar_feriados(camp, simular=not confirmar)
        return Response({'aplicado': confirmar and bool(cambios), 'cambios': cambios, 'sin_mover': sin_mover})

    @action(detail=True, methods=['get'])
    def calendario(self, request, pk=None):
        camp = self.get_object()
        qs = camp.fechas.prefetch_related('partidos__local', 'partidos__visitante', 'partidos__grupo',
                                          'partidos__eventos', 'partidos__medios', 'descansos__equipo')
        return Response(FechaCalendarioSerializer(qs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['get'], url_path='calendario-pdf')
    def calendario_pdf(self, request, pk=None):
        """PDF con el calendario completo (todas las fechas). Público si el campeonato está publicado."""
        camp = self.get_object()
        contenido = pdf.calendario_pdf(camp)
        nombre = slugify(f'calendario {camp.nombre} {camp.temporada}') or 'calendario'
        respuesta = HttpResponse(contenido, content_type='application/pdf')
        respuesta['Content-Disposition'] = f'attachment; filename="{nombre}.pdf"'
        return respuesta

    # --- eliminatoria
    @action(detail=True, methods=['post'], url_path='generar-eliminatoria')
    def generar_eliminatoria(self, request, pk=None):
        try:
            fecha = eliminatoria.generar_eliminatoria(self.get_object())
        except fixture.FixtureError as e:
            return _error(str(e))
        return Response(FechaSerializer(fecha).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='avanzar-eliminatoria')
    def avanzar_eliminatoria(self, request, pk=None):
        camp = self.get_object()
        try:
            fecha = eliminatoria.avanzar_eliminatoria(
                camp, confirmar_descanso=bool(request.data.get('confirmar_descanso', False)),
                semilla=request.data.get('semilla'))
        except fixture.RequiereConfirmacion as e:
            return _error(e.detalle, status.HTTP_409_CONFLICT, requiere_confirmacion=True)
        except fixture.FixtureError as e:
            return _error(str(e))
        return Response(FechaSerializer(fecha).data if fecha else {'detail': 'Campeonato finalizado.'})

    # --- consultas públicas
    @action(detail=True, methods=['get'], url_path='tabla')
    def tabla_posiciones(self, request, pk=None):
        camp = self.get_object()
        if camp.modalidad == Campeonato.Modalidad.RELAMPAGO and camp.partidos.exists():
            bloques = []  # eliminación directa: no hay tabla, sólo llaves
        elif camp.formato == Campeonato.Formato.LIGA:
            bloques = [{'grupo': None, 'filas': _serializar_tabla(tabla.calcular_tabla(camp), request)}]
        else:
            bloques = [{'grupo': g.nombre, 'filas': _serializar_tabla(tabla.calcular_tabla(camp, g), request)}
                       for g in camp.grupos.all()]
        return Response(bloques)

    @action(detail=True, methods=['get'])
    def goleadores(self, request, pk=None):
        camp = self.get_object()
        datos = tabla.goleadores(camp, int(request.query_params.get('limite', 20)))
        return Response([{
            'jugador': d['jugador'].id, 'nombre': d['jugador'].nombre_completo,
            'foto': request.build_absolute_uri(d['jugador'].foto.url) if d['jugador'].foto else None,
            'equipo': EquipoMiniSerializer(d['equipo'], context={'request': request}).data,
            'goles': d['goles'],
        } for d in datos])

    @action(detail=True, methods=['get'])
    def tarjetas(self, request, pk=None):
        """Ranking de tarjetas amarillas y rojas por jugador del campeonato."""
        camp = self.get_object()
        datos = tabla.tarjetas_jugadores(camp, int(request.query_params.get('limite', 30)))
        return Response([{
            'jugador': d['jugador'].id, 'nombre': d['jugador'].nombre_completo,
            'foto': request.build_absolute_uri(d['jugador'].foto.url) if d['jugador'].foto else None,
            'equipo': EquipoMiniSerializer(d['equipo'], context={'request': request}).data,
            'amarillas': d['amarillas'], 'rojas': d['rojas'],
        } for d in datos])

    @action(detail=True, methods=['get'])
    def resultados(self, request, pk=None):
        """Últimos resultados y próximos partidos del campeonato."""
        camp = self.get_object()
        ctx = {'request': request}
        limite = int(request.query_params.get('limite', 10))
        base = camp.partidos.select_related('local', 'visitante', 'fecha', 'grupo', 'campeonato').prefetch_related('eventos', 'medios')
        ultimos = base.filter(estado=Partido.Estado.FINALIZADO).order_by('-fecha_hora', '-fecha__numero', '-id')[:limite]
        proximos = base.exclude(estado=Partido.Estado.FINALIZADO).order_by('fecha__numero', 'fecha_hora', 'id')[:limite]
        return Response({
            'ultimos': PartidoSerializer(ultimos, many=True, context=ctx).data,
            'proximos': PartidoSerializer(proximos, many=True, context=ctx).data,
        })

    @action(detail=True, methods=['get'])
    def estadisticas(self, request, pk=None):
        camp = self.get_object()
        finalizados = camp.partidos.filter(estado=Partido.Estado.FINALIZADO)
        goles = sum(p.goles_local + p.goles_visitante for p in finalizados)
        eventos = EventoPartido.objects.filter(partido__campeonato=camp)
        return Response({
            'partidos_jugados': finalizados.count(),
            'partidos_totales': camp.partidos.count(),
            'goles': goles,
            'promedio_goles': round(goles / finalizados.count(), 2) if finalizados.count() else 0,
            'amarillas': eventos.filter(tipo__in=['AMARILLA', 'DOBLE_AMARILLA']).count(),
            'rojas': eventos.filter(tipo__in=['ROJA', 'DOBLE_AMARILLA']).count(),
        })


def _fecha(valor):
    if not valor:
        return None
    from datetime import date
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        raise fixture.FixtureError('Fecha de inicio inválida (use AAAA-MM-DD).')


class FeriadoViewSet(viewsets.ModelViewSet):
    """Feriados: lectura pública; sólo el Administrador los crea, edita o elimina."""
    queryset = Feriado.objects.select_related('campeonato')
    serializer_class = FeriadoSerializer
    roles_escritura = (ADMIN, ORGANIZADOR)
    search_fields = ['nombre']
    filterset_fields = ['campeonato', 'se_repite']

    def get_queryset(self):
        qs = super().get_queryset()
        if es_admin(self.request.user):
            return qs
        # los generales los ve cualquiera; los de un campeonato, quien lo gestiona (o el público si está publicado)
        return qs.filter(Q(campeonato__isnull=True) | Q(campeonato__in=alcance_campeonatos(self.request.user)))

    def _exigir_propio(self, feriado):
        if not es_admin(self.request.user) and feriado.campeonato_id is None:
            raise PermissionDenied('Los feriados generales sólo los modifica el administrador.')

    def update(self, request, *args, **kwargs):  # incluye PATCH
        self._exigir_propio(self.get_object())
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._exigir_propio(self.get_object())
        return super().destroy(request, *args, **kwargs)


class FechaViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = Fecha.objects.select_related('campeonato').prefetch_related('descansos__equipo')
    serializer_class = FechaSerializer
    roles_escritura = (ADMIN, ORGANIZADOR)
    filterset_fields = ['campeonato', 'fase']

    def get_queryset(self):
        return super().get_queryset().filter(campeonato__in=alcance_campeonatos(self.request.user).values('pk'))

    @action(detail=True, methods=['post'])
    def programar(self, request, pk=None):
        """Asigna día y hora a los partidos de una fecha/ronda: `{partidos: [{id, fecha_hora}]}`.

        Se usa sobre todo en semifinales y final: se crean sin fecha y se programan al terminar la ronda
        anterior. Sólo partidos pendientes.
        """
        fecha = self.get_object()
        items = request.data.get('partidos')
        if not isinstance(items, list) or not items:
            return _error('Indique la lista de partidos con su fecha y hora.')
        por_id = {p.id: p for p in fecha.partidos.all()}
        campo = serializers.DateTimeField()
        cambios = []
        for item in items:
            try:
                partido = por_id[int(item['id'])]
                momento = campo.to_internal_value(item.get('fecha_hora'))
            except (KeyError, TypeError, ValueError):
                return _error('Cada partido debe pertenecer a esta fecha y tener una fecha y hora válidas.')
            except serializers.ValidationError:
                return _error('Fecha y hora inválidas (use formato ISO, ej. 2026-11-03T19:00).')
            if partido.estado != Partido.Estado.PENDIENTE:
                return _error(f'{partido} ya no está pendiente: use «Reprogramar» o la edición del partido.')
            partido.fecha_hora = momento
            cambios.append(partido)
        if not request.data.get('confirmar_feriados'):
            calendario = CalendarioFeriados(fecha.campeonato)
            en_feriado = {}
            for partido in cambios:
                dia = timezone.localtime(partido.fecha_hora).date()
                if calendario.nombre(dia):
                    en_feriado[dia] = calendario.nombre(dia)
            if en_feriado:
                return _error('Hay partidos en día feriado. ¿Desea jugarlos igual?', status.HTTP_409_CONFLICT,
                              requiere_confirmacion_feriados=True,
                              feriados=[{'fecha': d.isoformat(), 'nombre': n} for d, n in sorted(en_feriado.items())])
        with transaction.atomic():
            for partido in cambios:
                partido.save(update_fields=['fecha_hora'])
            fecha.fecha_programada = timezone.localtime(min(p.fecha_hora for p in fecha.partidos.all()
                                                            if p.fecha_hora)).date()
            fecha.save(update_fields=['fecha_programada'])
        fecha = Fecha.objects.prefetch_related('partidos__local', 'partidos__visitante', 'partidos__grupo',
                                               'partidos__eventos', 'descansos__equipo').get(pk=fecha.pk)
        return Response(FechaCalendarioSerializer(fecha, context={'request': request}).data)


# ---------------------------------------------------------------- partidos (resultados en vivo)

class PartidoViewSet(viewsets.ModelViewSet):
    queryset = Partido.objects.select_related('campeonato', 'fecha', 'grupo', 'local', 'visitante') \
        .prefetch_related('eventos', 'medios')
    roles_escritura = (ADMIN, ORGANIZADOR, OPERADOR)
    filterset_fields = ['campeonato', 'fecha', 'estado', 'grupo', 'local', 'visitante']
    ordering_fields = ['fecha_hora', 'fecha__numero']

    def get_serializer_class(self):
        return PartidoDetalleSerializer if self.action == 'retrieve' else PartidoSerializer

    def get_queryset(self):
        qs = super().get_queryset().filter(campeonato__in=alcance_campeonatos(self.request.user).values('pk'))
        equipo = self.request.query_params.get('equipo')
        if equipo:
            qs = qs.filter(Q(local_id=equipo) | Q(visitante_id=equipo))
        return qs

    @action(detail=False, methods=['get'], url_path='en-vivo')
    def en_vivo(self, request):
        qs = self.get_queryset().filter(estado__in=[Partido.Estado.INICIADO, Partido.Estado.ENTRETIEMPO])
        return Response(PartidoSerializer(qs, many=True, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='cambiar-estado')
    def cambiar_estado(self, request, pk=None):
        """Cambia el estado y dispara la notificación push (vía señal)."""
        partido = self.get_object()
        estado = request.data.get('estado')
        if estado not in Partido.Estado.values:
            return _error(f'Estado inválido. Use: {", ".join(Partido.Estado.values)}')
        datos = {'estado': estado}
        if estado == Partido.Estado.POSTERGADO:
            # motivo obligatorio; `fecha_hora` es la nueva fecha (vacía = por definir)
            datos['motivo_postergacion'] = str(
                request.data.get('motivo') or request.data.get('motivo_postergacion') or '').strip()
            datos['fecha_hora'] = request.data.get('fecha_hora') or None
        elif request.data.get('fecha_hora'):
            datos['fecha_hora'] = request.data['fecha_hora']
        serializer = PartidoSerializer(partido, data=datos, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class EventoViewSet(viewsets.ModelViewSet):
    queryset = EventoPartido.objects.select_related('partido', 'equipo', 'jugador', 'jugador_relevo')
    serializer_class = EventoSerializer
    roles_escritura = (ADMIN, ORGANIZADOR, OPERADOR)
    filterset_fields = ['partido', 'tipo', 'equipo', 'jugador']

    def get_queryset(self):
        return super().get_queryset().filter(
            partido__campeonato__in=alcance_campeonatos(self.request.user).values('pk'))


class NotificacionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Notificacion.objects.select_related('campeonato', 'partido')
    serializer_class = NotificacionSerializer
    filterset_fields = ['campeonato', 'partido']

    def get_queryset(self):
        return super().get_queryset().filter(campeonato__in=alcance_campeonatos(self.request.user).values('pk'))
