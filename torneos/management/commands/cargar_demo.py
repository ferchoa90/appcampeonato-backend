import random

from django.core.management.base import BaseCommand

from torneos.models import Campeonato, Equipo, EventoPartido, Inscripcion, Jugador, Partido
from torneos.services import fixture

EQUIPOS = ['Águilas FC', 'Deportivo Norte', 'Real Sur', 'Atlético Centro', 'Unión Costa', 'Sporting Andes',
           'Racing Valle', 'Club Libertad']
NOMBRES = ['Juan', 'Luis', 'Carlos', 'Pedro', 'Diego', 'Mateo', 'Andrés', 'Kevin', 'Bryan', 'Jorge', 'Iván']
APELLIDOS = ['Pérez', 'Gómez', 'Rojas', 'Salazar', 'Vera', 'Mora', 'Cedeño', 'Loor', 'Zambrano', 'Bravo']
POSICIONES = ['POR'] + ['DEF'] * 4 + ['MED'] * 4 + ['DEL'] * 3


class Command(BaseCommand):
    help = 'Carga datos de demostración: 8 equipos con plantel y un campeonato con algunas fechas jugadas.'

    def handle(self, *args, **opts):
        rnd = random.Random(7)
        equipos = []
        for nombre in EQUIPOS:
            e, creado = Equipo.objects.get_or_create(nombre=nombre, defaults={
                'ciudad': 'Guayaquil', 'estadio': f'Estadio {nombre.split()[-1]}',
                'color_principal': '#%06x' % rnd.randint(0x204060, 0xb0b0b0)})
            equipos.append(e)
            if creado:
                for n, pos in enumerate(POSICIONES, start=1):
                    Jugador.objects.create(equipo=e, nombres=rnd.choice(NOMBRES), apellidos=rnd.choice(APELLIDOS),
                                           numero=n, posicion=pos)
        camp = Campeonato.objects.create(nombre='Liga Demo', temporada='2026', publicado=True, ida_vuelta=True)
        for e in equipos:
            Inscripcion.objects.create(campeonato=camp, equipo=e)
        fixture.generar_fixture(camp, semilla=1)
        for partido in camp.partidos.filter(fecha__numero__lte=3):
            for equipo in (partido.local, partido.visitante):
                for _ in range(rnd.choice([0, 0, 1, 2, 3])):
                    EventoPartido.objects.create(
                        partido=partido, tipo='GOL', equipo=equipo, minuto=rnd.randint(1, 90),
                        jugador=rnd.choice(list(equipo.jugadores.filter(posicion__in=['DEL', 'MED']))))
                if rnd.random() < 0.5:
                    EventoPartido.objects.create(partido=partido, tipo='AMARILLA', equipo=equipo,
                                                 minuto=rnd.randint(1, 90), jugador=equipo.jugadores.first())
            partido.refresh_from_db()
            partido.estado = Partido.Estado.FINALIZADO
            partido.save()
        self.stdout.write(self.style.SUCCESS(f'Demo lista: campeonato #{camp.id}'))
