from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from torneos.acceso import (campeonatos_gestionables, equipos_gestionables, es_admin, es_organizador, perfil_de)
from torneos.models import (TIPOS_NOTIFICACION, Campeonato, ConfiguracionSistema, Descanso, Dispositivo, Equipo, EventoPartido,
                            Fecha, Feriado, Grupo, Inscripcion, Jugador, Notificacion, Partido, PartidoMedio, PerfilUsuario,
                            PreferenciasNotificacion)
from torneos.permissions import ADMIN, AFICIONADO, OPERADOR, ORGANIZADOR
from torneos.services import medios as servicio_medios
from torneos.services.cifrado import cifrar
from torneos.services.tabla import tarjetas_por_equipo

User = get_user_model()


def _solicitante(serializer):
    request = serializer.context.get('request')
    return request.user if request else None


class EquipoMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Equipo
        fields = ['id', 'nombre', 'abreviatura', 'escudo', 'color_principal']


class EquipoSerializer(serializers.ModelSerializer):
    num_jugadores = serializers.IntegerField(source='jugadores.count', read_only=True)
    propietario_nombre = serializers.CharField(source='propietario.username', read_only=True, default=None)

    class Meta:
        model = Equipo
        fields = '__all__'
        validators = []  # la unicidad (por propietario) se valida en `validate`

    def validate(self, datos):
        usuario = _solicitante(self)
        if usuario is not None and not es_admin(usuario):
            datos.pop('propietario', None)  # sólo el Administrador elige el propietario
            propietario = self.instance.propietario if self.instance else usuario
        else:
            propietario = datos.get('propietario', self.instance.propietario if self.instance else None)
        nombre = datos.get('nombre', getattr(self.instance, 'nombre', None))
        repetido = Equipo.objects.filter(propietario=propietario, nombre__iexact=nombre)
        if self.instance:
            repetido = repetido.exclude(pk=self.instance.pk)
        if nombre and repetido.exists():
            raise serializers.ValidationError({'nombre': 'Ya existe un equipo con ese nombre.'})
        if self.instance and propietario != self.instance.propietario and self.instance.inscripciones.exists():
            raise serializers.ValidationError({'propietario': 'El equipo está inscrito en campeonatos: no se puede cambiar su propietario.'})
        if not self.instance:
            datos['propietario'] = propietario
        return datos


class JugadorSerializer(serializers.ModelSerializer):
    nombre_completo = serializers.CharField(read_only=True)
    equipo_nombre = serializers.CharField(source='equipo.nombre', read_only=True)
    posicion_nombre = serializers.CharField(source='get_posicion_display', read_only=True)

    class Meta:
        model = Jugador
        fields = '__all__'

    def validate_equipo(self, equipo):
        usuario = _solicitante(self)
        if usuario is not None and not equipos_gestionables(usuario).filter(pk=equipo.pk).exists():
            raise serializers.ValidationError('Ese equipo no existe o no le pertenece.')
        return equipo


class JugadorMiniSerializer(serializers.ModelSerializer):
    nombre_completo = serializers.CharField(read_only=True)

    class Meta:
        model = Jugador
        fields = ['id', 'nombre_completo', 'numero', 'foto', 'posicion']


class CampeonatoSerializer(serializers.ModelSerializer):
    num_equipos = serializers.IntegerField(source='inscripciones.count', read_only=True)
    num_partidos = serializers.IntegerField(source='partidos.count', read_only=True)
    formato_nombre = serializers.CharField(source='get_formato_display', read_only=True)
    propietario_nombre = serializers.CharField(source='propietario.username', read_only=True, default=None)

    class Meta:
        model = Campeonato
        fields = '__all__'
        read_only_fields = ['horarios_fijos']  # se cambian sólo con `campeonatos/{id}/horarios-fijos/`

    def validate_criterios_desempate(self, valor):
        validos = {'dif_gol', 'goles_favor', 'h2h', 'fair_play'}
        if not isinstance(valor, list) or not set(valor) <= validos:
            raise serializers.ValidationError(f'Use una lista con valores de: {", ".join(sorted(validos))}.')
        return valor

    def validate(self, datos):
        formato = datos.get('formato', getattr(self.instance, 'formato', 'LIGA'))
        if formato == Campeonato.Formato.LIGA:
            datos['num_grupos'] = 1
        elif datos.get('num_grupos', getattr(self.instance, 'num_grupos', 1)) < 2:
            raise serializers.ValidationError({'num_grupos': 'Un torneo por grupos necesita al menos 2 grupos.'})
        usuario = _solicitante(self)
        if usuario is not None and not es_admin(usuario):
            datos.pop('propietario', None)  # sólo el Administrador elige el propietario
            if not self.instance:
                datos['propietario'] = usuario
        elif 'propietario' in datos and self.instance and datos['propietario'] != self.instance.propietario \
                and self.instance.inscripciones.exists():
            raise serializers.ValidationError(
                {'propietario': 'El campeonato tiene equipos inscritos: no se puede cambiar su propietario.'})
        return datos


class FeriadoSerializer(serializers.ModelSerializer):
    campeonato_nombre = serializers.CharField(source='campeonato.nombre', read_only=True, default=None)

    class Meta:
        model = Feriado
        fields = ['id', 'fecha', 'nombre', 'campeonato', 'campeonato_nombre', 'se_repite']

    def validate_nombre(self, valor):
        valor = valor.strip()
        if not valor:
            raise serializers.ValidationError('Indique el nombre del feriado.')
        return valor

    def validate(self, datos):
        fecha = datos.get('fecha', getattr(self.instance, 'fecha', None))
        campeonato = datos.get('campeonato', getattr(self.instance, 'campeonato', None))
        usuario = _solicitante(self)
        if usuario is not None and not es_admin(usuario):
            if campeonato is None:
                raise serializers.ValidationError({'campeonato': 'Elija uno de sus campeonatos: los feriados generales los define el administrador.'})
            if not campeonatos_gestionables(usuario).filter(pk=campeonato.pk).exists():
                raise serializers.ValidationError({'campeonato': 'Ese campeonato no existe o no le pertenece.'})
        repetido = Feriado.objects.filter(fecha=fecha, campeonato=campeonato)
        if self.instance:
            repetido = repetido.exclude(pk=self.instance.pk)
        if fecha and repetido.exists():
            raise serializers.ValidationError('Ya existe un feriado para esa fecha en ese alcance.')
        return datos


class GrupoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Grupo
        fields = ['id', 'campeonato', 'nombre']


class InscripcionSerializer(serializers.ModelSerializer):
    equipo_detalle = EquipoMiniSerializer(source='equipo', read_only=True)
    grupo_nombre = serializers.CharField(source='grupo.nombre', read_only=True, default=None)

    class Meta:
        model = Inscripcion
        fields = ['id', 'campeonato', 'equipo', 'equipo_detalle', 'grupo', 'grupo_nombre']


class EventoSerializer(serializers.ModelSerializer):
    equipo_detalle = EquipoMiniSerializer(source='equipo', read_only=True)
    jugador_nombre = serializers.CharField(source='jugador.nombre_completo', read_only=True, default=None)
    relevo_nombre = serializers.CharField(source='jugador_relevo.nombre_completo', read_only=True, default=None)
    tipo_nombre = serializers.CharField(source='get_tipo_display', read_only=True)

    class Meta:
        model = EventoPartido
        fields = '__all__'

    def validate(self, datos):
        partido = datos.get('partido') or self.instance.partido
        equipo = datos.get('equipo') or self.instance.equipo
        usuario = _solicitante(self)
        if usuario is not None and not campeonatos_gestionables(usuario).filter(pk=partido.campeonato_id).exists():
            raise serializers.ValidationError({'partido': 'Ese partido no existe o no le pertenece.'})
        if equipo.id not in (partido.local_id, partido.visitante_id):
            raise serializers.ValidationError({'equipo': 'El equipo no juega este partido.'})
        for campo in ('jugador', 'jugador_relevo'):
            jugador = datos.get(campo)
            if jugador and jugador.equipo_id != equipo.id:
                raise serializers.ValidationError({campo: 'El jugador no pertenece al equipo del evento.'})
        tipo = datos.get('tipo') or self.instance.tipo
        if tipo == 'CAMBIO' and not (datos.get('jugador') and datos.get('jugador_relevo')):
            raise serializers.ValidationError('Un cambio requiere el jugador que sale y el que entra.')
        return datos


class MedioSerializer(serializers.ModelSerializer):
    """Foto o video de un partido. El archivo se envía en multipart; los límites los define el administrador."""
    archivo = serializers.FileField()
    nombre_archivo = serializers.SerializerMethodField()
    tipo = serializers.CharField(read_only=True)
    tamano = serializers.IntegerField(read_only=True)

    class Meta:
        model = PartidoMedio
        fields = ['id', 'partido', 'tipo', 'archivo', 'nombre_archivo', 'titulo', 'tamano', 'creado']
        read_only_fields = ['creado']

    def get_nombre_archivo(self, obj):
        return obj.archivo.name.rsplit('/', 1)[-1]

    def validate(self, datos):
        # al editar sólo se cambia el título: el archivo y el partido no se tocan
        if self.instance:
            datos.pop('archivo', None)
            datos.pop('partido', None)
            return datos
        archivo, partido = datos['archivo'], datos['partido']
        usuario = _solicitante(self)
        if usuario is not None and not campeonatos_gestionables(usuario).filter(pk=partido.campeonato_id).exists():
            raise serializers.ValidationError({'partido': 'Ese partido no existe o no le pertenece.'})
        try:
            datos['tipo'] = servicio_medios.validar(archivo, partido, ConfiguracionSistema.obtener())
        except servicio_medios.MedioError as e:
            raise serializers.ValidationError({'archivo': str(e)})
        datos['tamano'] = archivo.size
        return datos

    def create(self, datos):
        usuario = _solicitante(self)
        return PartidoMedio.objects.create(subido_por=usuario if usuario and usuario.is_authenticated else None, **datos)


class PartidoSerializer(serializers.ModelSerializer):
    local_detalle = EquipoMiniSerializer(source='local', read_only=True)
    visitante_detalle = EquipoMiniSerializer(source='visitante', read_only=True)
    campeonato_nombre = serializers.CharField(source='campeonato.nombre', read_only=True)
    fecha_numero = serializers.IntegerField(source='fecha.numero', read_only=True)
    fecha_nombre = serializers.CharField(source='fecha.nombre', read_only=True)
    fase = serializers.CharField(source='fecha.fase', read_only=True)
    grupo_nombre = serializers.CharField(source='grupo.nombre', read_only=True, default=None)
    estado_nombre = serializers.CharField(source='get_estado_display', read_only=True)
    tarjetas = serializers.SerializerMethodField()
    num_imagenes = serializers.SerializerMethodField()
    num_videos = serializers.SerializerMethodField()

    class Meta:
        model = Partido
        fields = '__all__'

    def get_num_imagenes(self, obj):
        return sum(1 for m in obj.medios.all() if m.tipo == PartidoMedio.Tipo.IMAGEN)

    def get_num_videos(self, obj):
        return sum(1 for m in obj.medios.all() if m.tipo == PartidoMedio.Tipo.VIDEO)

    def get_tarjetas(self, obj):
        """Amarillas y rojas de cada equipo en este partido (usa `eventos` precargados si los hay)."""
        totales = tarjetas_por_equipo(obj.eventos.all())
        def de(equipo_id):
            a, r = totales.get(equipo_id, (0, 0))
            return {'amarillas': a, 'rojas': r}
        return {'local': de(obj.local_id), 'visitante': de(obj.visitante_id)}

    def validate(self, datos):
        local = datos.get('local') or getattr(self.instance, 'local', None)
        visitante = datos.get('visitante') or getattr(self.instance, 'visitante', None)
        if local and visitante and local == visitante:
            raise serializers.ValidationError('Un equipo no puede jugar contra sí mismo.')
        usuario = _solicitante(self)
        if usuario is not None and not es_admin(usuario):
            campeonato = datos.get('campeonato')
            if campeonato is not None and not campeonatos_gestionables(usuario).filter(pk=campeonato.pk).exists():
                raise serializers.ValidationError({'campeonato': 'Ese campeonato no existe o no le pertenece.'})
            if es_organizador(usuario):
                for campo in ('local', 'visitante'):
                    equipo = datos.get(campo)
                    if equipo is not None and equipo.propietario_id != usuario.id:
                        raise serializers.ValidationError({campo: 'Ese equipo no existe o no le pertenece.'})
        estado = datos.get('estado', getattr(self.instance, 'estado', None))
        motivo = datos.get('motivo_postergacion', getattr(self.instance, 'motivo_postergacion', ''))
        if estado == Partido.Estado.POSTERGADO and not (motivo or '').strip():
            raise serializers.ValidationError({'motivo_postergacion': 'Indique el motivo de la postergación.'})
        return datos


class PartidoDetalleSerializer(PartidoSerializer):
    eventos = EventoSerializer(many=True, read_only=True)
    medios = MedioSerializer(many=True, read_only=True)

    class Meta(PartidoSerializer.Meta):
        pass


class DescansoSerializer(serializers.ModelSerializer):
    equipo_detalle = EquipoMiniSerializer(source='equipo', read_only=True)

    class Meta:
        model = Descanso
        fields = ['id', 'equipo', 'equipo_detalle', 'grupo']


class FechaSerializer(serializers.ModelSerializer):
    descansos = DescansoSerializer(many=True, read_only=True)
    fase_nombre = serializers.CharField(source='get_fase_display', read_only=True)

    class Meta:
        model = Fecha
        fields = '__all__'
        read_only_fields = ['campeonato', 'fase', 'numero']


class FechaCalendarioSerializer(FechaSerializer):
    partidos = PartidoSerializer(many=True, read_only=True)


class NotificacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notificacion
        fields = '__all__'


class UsuarioSerializer(serializers.ModelSerializer):
    """Usuarios del panel. El Administrador gestiona a todos; el Organizador sólo a los operadores que él crea
    (siempre limitados a sus campeonatos)."""
    rol = serializers.ChoiceField(choices=[ADMIN, OPERADOR, ORGANIZADOR], write_only=True, required=False)
    roles = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    campeonatos = serializers.PrimaryKeyRelatedField(many=True, queryset=Campeonato.objects.all(),
                                                     write_only=True, required=False)
    campeonatos_asignados = serializers.SerializerMethodField()
    creado_por_nombre = serializers.SerializerMethodField()
    restringido = serializers.SerializerMethodField()
    pendiente = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_active', 'password', 'rol', 'roles',
                  'campeonatos', 'campeonatos_asignados', 'creado_por_nombre', 'restringido', 'pendiente']

    def get_roles(self, user):
        if user.is_superuser:
            return [ADMIN]
        return list(user.groups.values_list('name', flat=True))

    def _perfil(self, user):
        return PerfilUsuario.objects.filter(user=user).first()

    def get_campeonatos_asignados(self, user):
        perfil = self._perfil(user)
        return [{'id': c.id, 'nombre': c.nombre} for c in perfil.campeonatos.all()] if perfil else []

    def get_creado_por_nombre(self, user):
        perfil = self._perfil(user)
        return perfil.creado_por.username if perfil and perfil.creado_por_id else None

    def get_restringido(self, user):
        perfil = self._perfil(user)
        return bool(perfil and perfil.restringido)

    def get_pendiente(self, user):
        """Cuenta creada por auto-registro que espera la aprobación de un administrador."""
        perfil = self._perfil(user)
        return (not user.is_active and not user.last_login and bool(perfil) and perfil.creado_por_id is None
                and user.groups.filter(name=ORGANIZADOR).exists())

    def validate(self, datos):
        solicitante = _solicitante(self)
        if solicitante is not None and es_organizador(solicitante):
            if datos.get('rol', OPERADOR) != OPERADOR:
                raise serializers.ValidationError({'rol': 'Sólo puede crear operadores.'})
            propios = set(campeonatos_gestionables(solicitante).values_list('pk', flat=True))
            ajenos = [c for c in datos.get('campeonatos', []) if c.pk not in propios]
            if ajenos:
                raise serializers.ValidationError({'campeonatos': 'Sólo puede asignar sus propios campeonatos.'})
        return datos

    def _asignar_rol(self, user, rol):
        if rol:
            user.groups.set([Group.objects.get_or_create(name=rol)[0]])

    def create(self, datos):
        solicitante = _solicitante(self)
        por_organizador = solicitante is not None and es_organizador(solicitante)
        rol = OPERADOR if por_organizador else datos.pop('rol', OPERADOR)
        datos.pop('rol', None)
        campeonatos = datos.pop('campeonatos', [])
        password = datos.pop('password', None)
        if not password:
            raise serializers.ValidationError({'password': 'La contraseña es obligatoria.'})
        user = User.objects.create_user(password=password, **datos)
        self._asignar_rol(user, rol)
        perfil = perfil_de(user)
        if por_organizador:
            perfil.creado_por = solicitante
        perfil.restringido = por_organizador or (rol == OPERADOR and bool(campeonatos))
        perfil.save()
        perfil.campeonatos.set(campeonatos)
        return user

    def update(self, user, datos):
        solicitante = _solicitante(self)
        por_organizador = solicitante is not None and es_organizador(solicitante)
        rol = datos.pop('rol', None)
        password = datos.pop('password', None)
        campeonatos = datos.pop('campeonatos', None)
        for k, v in datos.items():
            setattr(user, k, v)
        if password:
            user.set_password(password)
        user.save()
        if rol and not por_organizador:
            self._asignar_rol(user, rol)
        if campeonatos is not None:
            perfil = perfil_de(user)
            perfil.campeonatos.set(campeonatos)
            if not por_organizador and user.groups.filter(name=OPERADOR).exists():
                perfil.restringido = bool(campeonatos)
                perfil.save()
        return user


class RegistroSerializer(serializers.Serializer):
    """Auto-registro de un organizador: la cuenta queda inactiva hasta que un administrador la apruebe."""
    username = serializers.RegexField(r'^[\w.@+-]{3,150}$', error_messages={
        'invalid': 'Use de 3 a 150 caracteres: letras, números y . @ + - _'})
    password = serializers.CharField(write_only=True)
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_username(self, valor):
        if User.objects.filter(username__iexact=valor).exists():
            raise serializers.ValidationError('Ese usuario ya existe.')
        return valor

    def validate(self, datos):
        user = User(username=datos['username'], email=datos['email'], first_name=datos['first_name'])
        try:
            validate_password(datos['password'], user)
        except DjangoValidationError as e:
            raise serializers.ValidationError({'password': list(e.messages)})
        return datos

    def create(self, datos):
        user = User.objects.create_user(is_active=False, **datos)
        user.groups.set([Group.objects.get_or_create(name=ORGANIZADOR)[0]])
        perfil_de(user)
        return user


# ---------------------------------------------------------------- aficionados: intereses, dispositivos, cuenta

class PreferenciasSerializer(serializers.ModelSerializer):
    """Intereses de notificación: ninguna, todos los partidos o una selección de campeonatos y equipos."""
    campeonatos = serializers.PrimaryKeyRelatedField(many=True, queryset=Campeonato.objects.filter(publicado=True), required=False)
    equipos = serializers.PrimaryKeyRelatedField(many=True, queryset=Equipo.objects.all(), required=False)
    campeonatos_detalle = serializers.SerializerMethodField()
    equipos_detalle = serializers.SerializerMethodField()

    class Meta:
        model = PreferenciasNotificacion
        fields = ['modo', 'tipos', 'campeonatos', 'equipos', 'campeonatos_detalle', 'equipos_detalle']

    def get_campeonatos_detalle(self, obj):
        return [{'id': c.id, 'nombre': c.nombre} for c in obj.campeonatos.all()]

    def get_equipos_detalle(self, obj):
        return EquipoMiniSerializer(obj.equipos.all(), many=True, context=self.context).data

    def validate_tipos(self, valor):
        if not isinstance(valor, list) or not set(valor) <= set(TIPOS_NOTIFICACION):
            raise serializers.ValidationError(f'Use una lista con valores de: {", ".join(TIPOS_NOTIFICACION)}.')
        return sorted(set(valor), key=TIPOS_NOTIFICACION.index)

    def validate_equipos(self, equipos):
        # sólo equipos de campeonatos publicados (lo que el público puede ver)
        visibles = set(Equipo.objects.filter(inscripciones__campeonato__publicado=True).values_list('pk', flat=True))
        if any(e.pk not in visibles for e in equipos):
            raise serializers.ValidationError('Hay equipos que no están disponibles.')
        return equipos

    def update(self, instancia, datos):
        campeonatos, equipos = datos.pop('campeonatos', None), datos.pop('equipos', None)
        for k, v in datos.items():
            setattr(instancia, k, v)
        instancia.save()
        if campeonatos is not None:
            instancia.campeonatos.set(campeonatos)
        if equipos is not None:
            instancia.equipos.set(equipos)
        return instancia


class DispositivoSerializer(serializers.ModelSerializer):
    """Navegador (suscripción Web Push) o app móvil (token FCM) donde recibe los avisos."""
    suscripcion = serializers.JSONField(required=False, allow_null=True)
    token = serializers.CharField(required=False, allow_blank=True, max_length=600)

    class Meta:
        model = Dispositivo
        fields = ['id', 'tipo', 'token', 'suscripcion', 'nombre', 'activo', 'creado']
        read_only_fields = ['activo', 'creado']
        extra_kwargs = {'token': {'validators': []}}  # un token existente se reasigna al usuario actual

    def validate(self, datos):
        if datos['tipo'] == Dispositivo.Tipo.WEB:
            sus = datos.get('suscripcion') or {}
            claves = sus.get('keys') or {}
            if not (sus.get('endpoint') and claves.get('p256dh') and claves.get('auth')):
                raise serializers.ValidationError({'suscripcion': 'La suscripción del navegador no es válida.'})
            datos['token'] = sus['endpoint'][:600]
        elif not datos.get('token'):
            raise serializers.ValidationError({'token': 'Falta el token del dispositivo.'})
        return datos

    def create(self, datos):
        usuario = self.context['request'].user
        dispositivo, _ = Dispositivo.objects.update_or_create(
            token=datos['token'], defaults={'user': usuario, 'tipo': datos['tipo'], 'activo': True,
                                            'suscripcion': datos.get('suscripcion'), 'nombre': datos.get('nombre', '')[:120]})
        return dispositivo


class PerfilSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()
    tiene_clave = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'roles', 'tiene_clave']
        read_only_fields = ['username', 'email']

    def get_roles(self, user):
        return [ADMIN] if user.is_superuser else list(user.groups.values_list('name', flat=True))

    def get_tiene_clave(self, user):
        return user.has_usable_password()  # False en cuentas creadas con Google


class RegistroAficionadoSerializer(serializers.Serializer):
    """Registro de un aficionado con correo. Con verificación activa queda inactivo hasta confirmar el correo."""
    email = serializers.EmailField(max_length=150)
    password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_email(self, valor):
        valor = valor.strip().lower()
        if User.objects.filter(Q(username__iexact=valor) | Q(email__iexact=valor)).exists():
            raise serializers.ValidationError('Ya existe una cuenta con ese correo.')
        return valor

    def validate(self, datos):
        user = User(username=datos['email'], email=datos['email'], first_name=datos['first_name'])
        try:
            validate_password(datos['password'], user)
        except DjangoValidationError as e:
            raise serializers.ValidationError({'password': list(e.messages)})
        return datos


class ConfiguracionSerializer(serializers.ModelSerializer):
    """Configuración del sistema (sólo Administrador). La clave SMTP se recibe pero nunca se devuelve."""
    smtp_clave = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    tiene_clave = serializers.SerializerMethodField()
    correo_configurado = serializers.SerializerMethodField()

    class Meta:
        model = ConfiguracionSistema
        fields = ['smtp_host', 'smtp_puerto', 'smtp_usuario', 'smtp_clave', 'tiene_clave', 'smtp_seguridad', 'correo_remitente',
                  'correo_configurado', 'verificar_correo', 'url_publica', 'google_client_id', 'max_imagenes_partido',
                  'max_videos_partido', 'max_mb_imagen', 'max_mb_video', 'vapid_publica', 'vapid_contacto']
        read_only_fields = ['vapid_publica']

    def get_tiene_clave(self, obj):
        return bool(obj.smtp_clave)

    def get_correo_configurado(self, obj):
        return bool(obj.smtp_host and obj.correo_remitente)

    def validate_smtp_puerto(self, valor):
        if not 1 <= valor <= 65535:
            raise serializers.ValidationError('El puerto debe estar entre 1 y 65535.')
        return valor

    def validate(self, datos):
        for campo in ('max_imagenes_partido', 'max_videos_partido'):
            if datos.get(campo, 0) > 200:
                raise serializers.ValidationError({campo: 'El máximo permitido es 200.'})
        for campo, tope in (('max_mb_imagen', 50), ('max_mb_video', 2000)):
            if datos.get(campo, 1) < 1 or datos.get(campo, 1) > tope:
                raise serializers.ValidationError({campo: f'Use un peso entre 1 y {tope} MB.'})
        return datos

    def update(self, instancia, datos):
        clave = datos.pop('smtp_clave', None)
        if clave:  # vacío = conservar la actual
            instancia.smtp_clave = cifrar(clave)
        return super().update(instancia, datos)
