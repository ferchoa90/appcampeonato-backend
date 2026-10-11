from django.conf import settings
from django.db import models


def default_desempate():
    # Criterios tras los puntos (reglas FIFA): diferencia de gol, goles a favor,
    # resultados entre los empatados (h2h) y juego limpio. Al final, orden alfabético.
    return ['dif_gol', 'goles_favor', 'h2h', 'fair_play']


class Equipo(models.Model):
    nombre = models.CharField(max_length=100)
    # Dueño del equipo: vacío = del sistema (lo administra el Administrador); con valor = de un organizador.
    propietario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='equipos_propios')
    abreviatura = models.CharField(max_length=5, blank=True)
    ciudad = models.CharField(max_length=100, blank=True)
    estadio = models.CharField(max_length=120, blank=True)
    entrenador = models.CharField(max_length=120, blank=True)
    color_principal = models.CharField(max_length=7, blank=True, default='#1f3a5f')
    escudo = models.ImageField(upload_to='escudos/', blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['nombre']
        constraints = [
            # el nombre es único por propietario (y entre los del sistema)
            models.UniqueConstraint(fields=['propietario', 'nombre'], name='equipo_nombre_unico_por_propietario'),
            models.UniqueConstraint(fields=['nombre'], condition=models.Q(propietario__isnull=True),
                                    name='equipo_nombre_unico_del_sistema'),
        ]

    def __str__(self):
        return self.nombre

    def save(self, *args, **kwargs):
        if not self.abreviatura:
            self.abreviatura = self.nombre[:3].upper()
        super().save(*args, **kwargs)


class Jugador(models.Model):
    class Posicion(models.TextChoices):
        PORTERO = 'POR', 'Portero'
        DEFENSA = 'DEF', 'Defensa'
        MEDIOCAMPISTA = 'MED', 'Mediocampista'
        DELANTERO = 'DEL', 'Delantero'

    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name='jugadores')
    nombres = models.CharField(max_length=100)
    apellidos = models.CharField(max_length=100)
    numero = models.PositiveSmallIntegerField(null=True, blank=True)
    posicion = models.CharField(max_length=3, choices=Posicion.choices, default=Posicion.MEDIOCAMPISTA)
    fecha_nacimiento = models.DateField(null=True, blank=True)
    nacionalidad = models.CharField(max_length=60, blank=True)
    identificacion = models.CharField(max_length=20, blank=True)
    foto = models.ImageField(upload_to='jugadores/', blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ['equipo__nombre', 'numero', 'apellidos']
        verbose_name_plural = 'jugadores'

    def __str__(self):
        return f'{self.nombres} {self.apellidos}'

    @property
    def nombre_completo(self):
        return f'{self.nombres} {self.apellidos}'.strip()


class Campeonato(models.Model):
    class Formato(models.TextChoices):
        LIGA = 'LIGA', 'Sin grupos (liga o relámpago)'
        GRUPOS = 'GRUPOS', 'Fase de grupos + eliminatoria'

    class Modalidad(models.TextChoices):
        IDA = 'IDA', 'Solo ida'
        IDA_VUELTA = 'IDA_VUELTA', 'Ida y vuelta'
        RELAMPAGO = 'RELAMPAGO', 'Relámpago (el que pierde se elimina)'

    class Estado(models.TextChoices):
        BORRADOR = 'BORRADOR', 'Borrador'
        EN_CURSO = 'EN_CURSO', 'En curso'
        FINALIZADO = 'FINALIZADO', 'Finalizado'

    nombre = models.CharField(max_length=150)
    # Dueño del campeonato: vacío = del sistema (sólo el Administrador); con valor = de un organizador.
    propietario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name='campeonatos_propios')
    temporada = models.CharField(max_length=20, blank=True)
    descripcion = models.TextField(blank=True)
    formato = models.CharField(max_length=10, choices=Formato.choices, default=Formato.LIGA)
    # Se elige al hacer el sorteo; ida_vuelta se mantiene sincronizado para los clientes existentes.
    modalidad = models.CharField(max_length=12, choices=Modalidad.choices, default=Modalidad.IDA)
    ida_vuelta = models.BooleanField(default=False)
    # Programación de partidos (se fija al sortear y se reutiliza en las rondas siguientes)
    dias_semana = models.JSONField(default=list, blank=True, help_text='Días de juego: 1=lunes ... 7=domingo')
    partidos_por_dia = models.PositiveSmallIntegerField(null=True, blank=True,
                                                         help_text='Vacío: todos los partidos de la fecha el mismo día')
    hora_inicio = models.TimeField(null=True, blank=True)
    minutos_entre_partidos = models.PositiveSmallIntegerField(default=90)
    dias_entre_fechas = models.PositiveSmallIntegerField(default=7, help_text='Se usa si no hay días de la semana')
    # Reglas de logística: [{"equipo": id, "orden": 2}] = los partidos de ese equipo siempre en el 2.º horario del día
    horarios_fijos = models.JSONField(default=list, blank=True)
    num_grupos = models.PositiveSmallIntegerField(default=1)
    clasificados_por_grupo = models.PositiveSmallIntegerField(default=2)
    puntos_victoria = models.PositiveSmallIntegerField(default=3)
    puntos_empate = models.PositiveSmallIntegerField(default=1)
    puntos_derrota = models.PositiveSmallIntegerField(default=0)
    criterios_desempate = models.JSONField(default=default_desempate)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    logo = models.ImageField(upload_to='campeonatos/', blank=True, null=True)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.BORRADOR)
    publicado = models.BooleanField(default=False, help_text='Visible para el público y la app')
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado']

    def __str__(self):
        return f'{self.nombre} {self.temporada}'.strip()


class Feriado(models.Model):
    """Día no laborable. Sin campeonato aplica a todos; con campeonato, sólo a ese."""
    fecha = models.DateField()
    nombre = models.CharField(max_length=100)
    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='feriados', help_text='Vacío: aplica a todos los campeonatos')
    se_repite = models.BooleanField(default=False, help_text='Se repite todos los años (mismo día y mes)')

    class Meta:
        ordering = ['fecha', 'nombre']

    def __str__(self):
        return f'{self.nombre} ({self.fecha:%d/%m/%Y})'


class Grupo(models.Model):
    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, related_name='grupos')
    nombre = models.CharField(max_length=30)

    class Meta:
        ordering = ['nombre']
        unique_together = [('campeonato', 'nombre')]

    def __str__(self):
        return f'{self.campeonato} - {self.nombre}'


class Inscripcion(models.Model):
    """Equipo inscrito en un campeonato (y, si aplica, asignado a un grupo)."""
    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, related_name='inscripciones')
    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name='inscripciones')
    grupo = models.ForeignKey(Grupo, on_delete=models.SET_NULL, null=True, blank=True, related_name='inscripciones')

    class Meta:
        unique_together = [('campeonato', 'equipo')]
        verbose_name_plural = 'inscripciones'

    def __str__(self):
        return f'{self.equipo} en {self.campeonato}'


class Fecha(models.Model):
    """Jornada del campeonato (fecha 1, fecha 2, ... o ronda de eliminatoria)."""
    class Fase(models.TextChoices):
        LIGA = 'LIGA', 'Liga'
        GRUPOS = 'GRUPOS', 'Fase de grupos'
        RONDA = 'RONDA', 'Ronda eliminatoria'
        OCTAVOS = 'OCTAVOS', 'Octavos de final'
        CUARTOS = 'CUARTOS', 'Cuartos de final'
        SEMIFINAL = 'SEMIFINAL', 'Semifinal'
        FINAL = 'FINAL', 'Final'

    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, related_name='fechas')
    fase = models.CharField(max_length=10, choices=Fase.choices, default=Fase.LIGA)
    numero = models.PositiveSmallIntegerField()
    nombre = models.CharField(max_length=60, blank=True)
    fecha_programada = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['campeonato', 'numero']
        unique_together = [('campeonato', 'numero')]

    def __str__(self):
        return self.nombre or f'Fecha {self.numero}'


class Descanso(models.Model):
    """Equipo que no juega en una fecha (cuando el número de equipos es impar)."""
    fecha = models.ForeignKey(Fecha, on_delete=models.CASCADE, related_name='descansos')
    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name='descansos')
    grupo = models.ForeignKey(Grupo, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = [('fecha', 'equipo')]


class Partido(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = 'PENDIENTE', 'Pendiente'
        INICIADO = 'INICIADO', 'Iniciado'
        ENTRETIEMPO = 'ENTRETIEMPO', 'Entretiempo'
        FINALIZADO = 'FINALIZADO', 'Finalizado'
        POSTERGADO = 'POSTERGADO', 'Postergado'

    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, related_name='partidos')
    fecha = models.ForeignKey(Fecha, on_delete=models.CASCADE, related_name='partidos')
    grupo = models.ForeignKey(Grupo, on_delete=models.SET_NULL, null=True, blank=True, related_name='partidos')
    local = models.ForeignKey(Equipo, on_delete=models.PROTECT, related_name='partidos_local')
    visitante = models.ForeignKey(Equipo, on_delete=models.PROTECT, related_name='partidos_visitante')
    fecha_hora = models.DateTimeField(null=True, blank=True)
    estadio = models.CharField(max_length=120, blank=True)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.PENDIENTE)
    goles_local = models.PositiveSmallIntegerField(default=0)
    goles_visitante = models.PositiveSmallIntegerField(default=0)
    penales_local = models.PositiveSmallIntegerField(null=True, blank=True)
    penales_visitante = models.PositiveSmallIntegerField(null=True, blank=True)
    es_vuelta = models.BooleanField(default=False)
    observaciones = models.TextField(blank=True)
    # Postergación: motivo visible al público; `fecha_hora` pasa a ser la nueva fecha (o queda vacía:
    # por definir) y `fecha_original` conserva cuándo estaba programado antes de postergarse.
    motivo_postergacion = models.CharField(max_length=255, blank=True)
    fecha_original = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['fecha__numero', 'fecha_hora', 'id']

    def __str__(self):
        return f'{self.local} vs {self.visitante}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Se lee de __dict__ y no con getattr: si el campo está diferido (p. ej. en un borrado en cascada)
        # acceder a él dispara una recarga que vuelve a entrar aquí y provoca recursión infinita.
        cargado = self.__dict__
        self._estado_previo = cargado.get('estado') if self.pk else None
        self._fecha_hora_previa = cargado.get('fecha_hora') if self.pk else None
        self._marcador_previo = (cargado.get('goles_local'), cargado.get('goles_visitante')) if self.pk else None

    def save(self, *args, **kwargs):
        entra_en_postergado = self.estado == self.Estado.POSTERGADO and self._estado_previo != self.Estado.POSTERGADO
        if entra_en_postergado and not self.fecha_original and self._fecha_hora_previa \
                and self._fecha_hora_previa != self.fecha_hora:
            self.fecha_original = self._fecha_hora_previa
            if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
                kwargs['update_fields'] = list(kwargs['update_fields']) + ['fecha_original']
        super().save(*args, **kwargs)
        self._fecha_hora_previa = self.fecha_hora

    @property
    def ganador(self):
        """Equipo ganador (considera penales si hay empate); None si empate o sin finalizar."""
        if self.estado != self.Estado.FINALIZADO:
            return None
        if self.goles_local != self.goles_visitante:
            return self.local if self.goles_local > self.goles_visitante else self.visitante
        if self.penales_local is not None and self.penales_visitante is not None \
                and self.penales_local != self.penales_visitante:
            return self.local if self.penales_local > self.penales_visitante else self.visitante
        return None


class EventoPartido(models.Model):
    class Tipo(models.TextChoices):
        GOL = 'GOL', 'Gol'
        GOL_PENAL = 'GOL_PENAL', 'Gol de penal'
        AUTOGOL = 'AUTOGOL', 'Autogol'
        AMARILLA = 'AMARILLA', 'Tarjeta amarilla'
        DOBLE_AMARILLA = 'DOBLE_AMARILLA', 'Doble amarilla'
        ROJA = 'ROJA', 'Tarjeta roja'
        CAMBIO = 'CAMBIO', 'Cambio'
        INCIDENCIA = 'INCIDENCIA', 'Incidencia'

    partido = models.ForeignKey(Partido, on_delete=models.CASCADE, related_name='eventos')
    tipo = models.CharField(max_length=15, choices=Tipo.choices)
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, related_name='eventos')
    jugador = models.ForeignKey(Jugador, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='eventos', help_text='Autor; en un cambio, quien sale')
    jugador_relevo = models.ForeignKey(Jugador, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='eventos_relevo', help_text='En un cambio, quien entra')
    minuto = models.PositiveSmallIntegerField(default=0)
    descripcion = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['minuto', 'id']

    GOLES = ('GOL', 'GOL_PENAL', 'AUTOGOL')


class PerfilUsuario(models.Model):
    """Datos de acceso de un usuario más allá del rol (grupo).

    Un Operador sin restricción (los que ya existían) opera todos los campeonatos. Los operadores que crea un
    organizador son `restringido=True` y sólo operan los campeonatos asignados en `campeonatos`.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='perfil')
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='usuarios_creados')
    restringido = models.BooleanField(default=False)
    campeonatos = models.ManyToManyField(Campeonato, blank=True, related_name='operadores')

    def __str__(self):
        return f'Perfil de {self.user}'


class Notificacion(models.Model):
    """Registro de las notificaciones push emitidas (también sirve de bandeja en la app)."""
    campeonato = models.ForeignKey(Campeonato, on_delete=models.CASCADE, related_name='notificaciones')
    partido = models.ForeignKey(Partido, on_delete=models.CASCADE, null=True, blank=True, related_name='notificaciones')
    tipo = models.CharField(max_length=10, default='ESTADO')  # ESTADO | GOL | TARJETA | CAMBIO | INCIDENCIA
    titulo = models.CharField(max_length=150)
    cuerpo = models.CharField(max_length=255)
    enviada = models.BooleanField(default=False)
    destinatarios = models.PositiveIntegerField(default=0, help_text='Dispositivos de aficionados notificados')
    error = models.CharField(max_length=255, blank=True)
    creada = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creada']


TIPOS_NOTIFICACION = ['ESTADO', 'GOL', 'TARJETA', 'CAMBIO', 'INCIDENCIA']


class PreferenciasNotificacion(models.Model):
    """Intereses de un aficionado (o cualquier usuario): qué notificaciones push quiere recibir."""

    class Modo(models.TextChoices):
        NADA = 'NADA', 'Ninguna'
        TODO = 'TODO', 'Todos los partidos'
        SELECCION = 'SELECCION', 'Solo lo que selecciono'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preferencias')
    modo = models.CharField(max_length=10, choices=Modo.choices, default=Modo.NADA)
    tipos = models.JSONField(default=list, blank=True, help_text='Tipos de movimiento: ESTADO, GOL, TARJETA, CAMBIO, INCIDENCIA')
    campeonatos = models.ManyToManyField(Campeonato, blank=True, related_name='seguidores')
    equipos = models.ManyToManyField(Equipo, blank=True, related_name='seguidores')

    def __str__(self):
        return f'Preferencias de {self.user}'


class Dispositivo(models.Model):
    """Dónde recibe notificaciones un usuario: un navegador (Web Push) o la app móvil (token FCM)."""

    class Tipo(models.TextChoices):
        WEB = 'WEB', 'Navegador'
        FCM = 'FCM', 'App móvil'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dispositivos')
    tipo = models.CharField(max_length=3, choices=Tipo.choices)
    token = models.CharField(max_length=600, unique=True, help_text='Endpoint (web) o token FCM')
    suscripcion = models.JSONField(null=True, blank=True, help_text='Suscripción Web Push (endpoint y claves)')
    nombre = models.CharField(max_length=120, blank=True)
    activo = models.BooleanField(default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado']

    def __str__(self):
        return f'{self.get_tipo_display()} de {self.user}'


def ruta_medio(instance, nombre):
    return f'partidos/{instance.partido_id}/{nombre}'


class PartidoMedio(models.Model):
    """Foto o video de un partido, subido por el administrador (o el organizador del campeonato)."""

    class Tipo(models.TextChoices):
        IMAGEN = 'IMAGEN', 'Imagen'
        VIDEO = 'VIDEO', 'Video'

    partido = models.ForeignKey(Partido, on_delete=models.CASCADE, related_name='medios')
    tipo = models.CharField(max_length=6, choices=Tipo.choices)
    archivo = models.FileField(upload_to=ruta_medio)
    titulo = models.CharField(max_length=150, blank=True)
    tamano = models.PositiveBigIntegerField(default=0, help_text='Bytes')
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='medios_subidos')
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['tipo', 'creado']

    def __str__(self):
        return f'{self.get_tipo_display()} de {self.partido}'


class ConfiguracionSistema(models.Model):
    """Configuración global (un solo registro): correo, cuentas, límites de medios y claves de notificaciones web."""

    class Seguridad(models.TextChoices):
        NINGUNA = 'NINGUNA', 'Ninguna'
        STARTTLS = 'STARTTLS', 'STARTTLS (puerto 587)'
        SSL = 'SSL', 'SSL/TLS (puerto 465)'

    # correo saliente (la clave se guarda cifrada)
    smtp_host = models.CharField(max_length=200, blank=True)
    smtp_puerto = models.PositiveIntegerField(default=587)
    smtp_usuario = models.CharField(max_length=200, blank=True)
    smtp_clave = models.TextField(blank=True)
    smtp_seguridad = models.CharField(max_length=10, choices=Seguridad.choices, default=Seguridad.STARTTLS)
    correo_remitente = models.CharField(max_length=200, blank=True, help_text='Ej. GoalScore <notificaciones@midominio.com>')
    # cuentas de aficionados
    verificar_correo = models.BooleanField(default=True)
    url_publica = models.CharField(max_length=200, blank=True, help_text='Dirección del portal, para los enlaces de los correos')
    google_client_id = models.CharField(max_length=200, blank=True)
    # límites de fotos y videos por partido
    max_imagenes_partido = models.PositiveIntegerField(default=10)
    max_videos_partido = models.PositiveIntegerField(default=2)
    max_mb_imagen = models.PositiveIntegerField(default=5)
    max_mb_video = models.PositiveIntegerField(default=100)
    # notificaciones del navegador (Web Push / VAPID)
    vapid_publica = models.CharField(max_length=200, blank=True)
    vapid_privada = models.TextField(blank=True)
    vapid_contacto = models.CharField(max_length=200, blank=True, help_text='Ej. mailto:soporte@midominio.com')

    class Meta:
        verbose_name = 'configuración'
        verbose_name_plural = 'configuración'

    @classmethod
    def obtener(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def save(self, *args, **kwargs):
        self.pk = 1  # registro único
        super().save(*args, **kwargs)
