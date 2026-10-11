from django.contrib import admin

from torneos import models

for modelo in (models.Equipo, models.Jugador, models.Campeonato, models.Grupo, models.Inscripcion,
               models.Fecha, models.Feriado, models.Descanso, models.Partido, models.EventoPartido, models.Notificacion):
    admin.site.register(modelo)
