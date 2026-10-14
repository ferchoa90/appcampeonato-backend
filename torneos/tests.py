import json
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APITestCase

from torneos.models import (Campeonato, ConfiguracionSistema, Dispositivo, Equipo, EventoPartido, Inscripcion, Jugador,
                            Notificacion, Partido, PerfilUsuario, PreferenciasNotificacion)
from torneos.services import fixture, tabla
from torneos.services.eliminatoria import avanzar_eliminatoria, generar_eliminatoria


def crear_equipos(n, prefijo='Equipo'):
    return [Equipo.objects.create(nombre=f'{prefijo} {i}') for i in range(n)]


def crear_campeonato(n, prefijo='Equipo', **kw):
    camp = Campeonato.objects.create(nombre='Liga Test', **kw)
    for e in crear_equipos(n, prefijo):
        Inscripcion.objects.create(campeonato=camp, equipo=e)
    return camp


def jugar(partido, gl, gv):
    partido.goles_local, partido.goles_visitante, partido.estado = gl, gv, Partido.Estado.FINALIZADO
    partido.save()


class FixtureTests(APITestCase):
    def test_par_todos_juegan_una_vez_contra_todos(self):
        camp = crear_campeonato(6)
        fixture.generar_fixture(camp)
        self.assertEqual(camp.fechas.count(), 5)
        self.assertEqual(camp.partidos.count(), 15)
        pares = {frozenset((p.local_id, p.visitante_id)) for p in camp.partidos.all()}
        self.assertEqual(len(pares), 15)
        for fecha in camp.fechas.all():  # nadie juega dos veces en una fecha
            ids = [e for p in fecha.partidos.all() for e in (p.local_id, p.visitante_id)]
            self.assertEqual(len(ids), len(set(ids)))

    def test_ida_y_vuelta(self):
        camp = crear_campeonato(4)
        fixture.generar_fixture(camp, ida_vuelta=True)
        self.assertEqual(camp.partidos.count(), 12)
        self.assertEqual(camp.fechas.count(), 6)
        self.assertEqual(camp.partidos.filter(es_vuelta=True).count(), 6)

    def test_impares_piden_confirmacion_y_generan_descansos(self):
        camp = crear_campeonato(5)
        with self.assertRaises(fixture.RequiereConfirmacion):
            fixture.generar_fixture(camp)
        fixture.generar_fixture(camp, confirmar_descanso=True)
        self.assertEqual(camp.fechas.count(), 5)
        self.assertEqual(camp.partidos.count(), 10)
        descansos = [d.equipo_id for f in camp.fechas.all() for d in f.descansos.all()]
        self.assertEqual(len(descansos), 5)
        self.assertEqual(len(set(descansos)), 5)  # cada equipo descansa exactamente una vez

    def test_no_regenera_sin_permiso(self):
        camp = crear_campeonato(4)
        fixture.generar_fixture(camp)
        with self.assertRaises(fixture.FixtureError):
            fixture.generar_fixture(camp)
        fixture.generar_fixture(camp, regenerar=True)
        self.assertEqual(camp.partidos.count(), 6)


class TablaTests(APITestCase):
    def setUp(self):
        self.camp = crear_campeonato(3)
        self.a, self.b, self.c = [i.equipo for i in self.camp.inscripciones.order_by('equipo__nombre')]
        fixture.generar_fixture(self.camp, confirmar_descanso=True)

    def partido(self, x, y):
        return Partido.objects.get(local__in=[x, y], visitante__in=[x, y])

    def test_puntos_fifa(self):
        p = self.partido(self.a, self.b)
        jugar(p, 2, 0) if p.local == self.a else jugar(p, 0, 2)
        t = {f['equipo'].nombre: f for f in tabla.calcular_tabla(self.camp)}
        self.assertEqual(t['Equipo 0']['pts'], 3)
        self.assertEqual(t['Equipo 1']['pts'], 0)
        self.assertEqual(t['Equipo 0']['dg'], 2)
        self.assertEqual(t['Equipo 2']['pj'], 0)

    def test_desempate_por_diferencia_de_gol(self):
        for x, y, gx, gy in [(self.a, self.b, 3, 0), (self.a, self.c, 0, 1), (self.b, self.c, 1, 0)]:
            p = self.partido(x, y)
            jugar(p, gx, gy) if p.local == x else jugar(p, gy, gx)
        # a: 3 pts (+2), b: 3 pts (-2), c: 3 pts (0) -> h2h cíclico, luego dif. de gol global
        orden = [f['equipo'].nombre for f in tabla.calcular_tabla(self.camp)]
        self.assertEqual(orden[0], 'Equipo 0')
        self.assertEqual(orden[-1], 'Equipo 1')


class EventosYNotificacionesTests(APITestCase):
    def setUp(self):
        self.camp = crear_campeonato(2)
        fixture.generar_fixture(self.camp)
        self.p = self.camp.partidos.first()
        self.j = Jugador.objects.create(equipo=self.p.local, nombres='Juan', apellidos='Perez', numero=9)

    def test_marcador_se_deriva_de_los_goles(self):
        EventoPartido.objects.create(partido=self.p, tipo='GOL', equipo=self.p.local, jugador=self.j, minuto=10)
        EventoPartido.objects.create(partido=self.p, tipo='AUTOGOL', equipo=self.p.local, minuto=20)
        self.p.refresh_from_db()
        self.assertEqual((self.p.goles_local, self.p.goles_visitante), (1, 1))

    def test_cambio_de_estado_genera_notificacion(self):
        self.p.estado = 'INICIADO'
        self.p.save()
        self.p.estado = 'FINALIZADO'
        self.p.save()
        self.assertEqual(Notificacion.objects.filter(partido=self.p).count(), 2)

    def test_goleadores(self):
        for _ in range(2):
            EventoPartido.objects.create(partido=self.p, tipo='GOL', equipo=self.p.local, jugador=self.j)
        self.assertEqual(tabla.goleadores(self.camp)[0]['goles'], 2)


class EliminatoriaTests(APITestCase):
    def test_grupos_y_llaves(self):
        camp = crear_campeonato(8, formato='GRUPOS', num_grupos=2, clasificados_por_grupo=2)
        fixture.sortear_grupos(camp)
        fixture.generar_fixture(camp)
        for p in camp.partidos.all():
            jugar(p, 1, 0)
        semis = generar_eliminatoria(camp)
        self.assertEqual(semis.fase, 'SEMIFINAL')
        self.assertEqual(semis.partidos.count(), 2)
        for p in semis.partidos.all():
            jugar(p, 2, 1)
        final = avanzar_eliminatoria(camp)
        self.assertEqual(final.fase, 'FINAL')
        jugar(final.partidos.first(), 1, 0)
        self.assertIsNone(avanzar_eliminatoria(camp))
        camp.refresh_from_db()
        self.assertEqual(camp.estado, 'FINALIZADO')

    # ---- cuadro: la posición (llave) manda, no la hora programada
    def campeonato_de_grupos(self, equipos, grupos, clasificados=2):
        camp = crear_campeonato(equipos, formato='GRUPOS', num_grupos=grupos, clasificados_por_grupo=clasificados)
        fixture.sortear_grupos(camp, semilla=7)
        fixture.generar_fixture(camp)
        for p in camp.partidos.all():
            jugar(p, 1, 0)   # gana siempre el local
        return camp

    @staticmethod
    def grupo_de(camp, equipo):
        return Inscripcion.objects.get(campeonato=camp, equipo=equipo).grupo.nombre

    def test_cruce_clasico_ubica_al_1_y_2_de_cada_grupo_en_mitades_opuestas(self):
        camp = self.campeonato_de_grupos(16, 4)
        cuartos = generar_eliminatoria(camp)
        self.assertEqual(cuartos.fase, 'CUARTOS')
        partidos = list(cuartos.partidos.order_by('llave'))
        self.assertEqual([p.llave for p in partidos], [1, 2, 3, 4])
        g = lambda p: (self.grupo_de(camp, p.local), self.grupo_de(camp, p.visitante))  # noqa: E731
        nombres = sorted({self.grupo_de(camp, e) for e in camp.inscripciones.values_list('equipo', flat=True)})
        a, b, c, d = nombres
        self.assertEqual([g(p) for p in partidos], [(a, b), (c, d), (b, a), (d, c)])   # 1A-2B, 1C-2D, 1B-2A, 1D-2C
        # hasta la final nadie se reencuentra con alguien de su grupo
        ronda = cuartos
        while ronda.fase != 'FINAL':
            for p in ronda.partidos.all():
                jugar(p, 2, 1)
            ronda = avanzar_eliminatoria(camp)
            if ronda.fase != 'FINAL':
                for p in ronda.partidos.all():
                    self.assertNotEqual(self.grupo_de(camp, p.local), self.grupo_de(camp, p.visitante))

    def test_los_ganadores_se_cruzan_por_llave_aunque_el_horario_sea_otro(self):
        from datetime import timedelta
        from django.utils import timezone
        camp = self.campeonato_de_grupos(16, 4)
        cuartos = generar_eliminatoria(camp)
        base = timezone.now() + timedelta(days=3)
        por_llave = {p.llave: p for p in cuartos.partidos.all()}
        # se programa al revés: la llave 4 se juega primero y la 1 al final
        for llave, p in por_llave.items():
            p.fecha_hora = base + timedelta(hours=5 - llave)
            p.save()
        for p in por_llave.values():
            jugar(p, 3, 0)
        semis = avanzar_eliminatoria(camp)
        ganadores = {llave: por_llave[llave].local_id for llave in por_llave}
        cruces = {p.llave: {p.local_id, p.visitante_id} for p in semis.partidos.all()}
        self.assertEqual(cruces, {1: {ganadores[1], ganadores[2]}, 2: {ganadores[3], ganadores[4]}})

    def test_siembra_estandar_cuando_no_es_el_cruce_clasico(self):
        from torneos.services.eliminatoria import _cuadro_por_siembra, _orden_siembra
        self.assertEqual(_orden_siembra(2), [1, 2])
        self.assertEqual(_orden_siembra(4), [1, 4, 2, 3])
        self.assertEqual(_orden_siembra(8), [1, 8, 4, 5, 2, 7, 3, 6])
        self.assertEqual(_orden_siembra(16), [1, 16, 8, 9, 4, 13, 5, 12, 2, 15, 7, 10, 3, 14, 6, 11])
        cabezas = list('ABCDEFGH')
        # 1 contra 8, 4 contra 5, 2 contra 7, 3 contra 6; los dos mejores sólo se cruzarían en la final
        self.assertEqual(_cuadro_por_siembra(cabezas),
                         [('A', 'H'), ('D', 'E'), ('B', 'G'), ('C', 'F')])
        # 4 grupos con 1 clasificado cada uno = semifinal; 8 grupos con 1 = cuartos con siembra estándar
        camp = self.campeonato_de_grupos(16, 8, clasificados=1)
        cuartos = generar_eliminatoria(camp)
        self.assertEqual((cuartos.fase, cuartos.partidos.count()), ('CUARTOS', 4))
        self.assertEqual(sorted(p.llave for p in cuartos.partidos.all()), [1, 2, 3, 4])

    def test_las_rondas_ya_creadas_reciben_su_llave_al_migrar(self):
        camp = self.campeonato_de_grupos(8, 2)
        semis = generar_eliminatoria(camp)
        self.assertEqual(sorted(p.llave for p in semis.partidos.all()), [1, 2])


class RelampagoTests(APITestCase):
    def jugar_ronda(self, fecha):
        for p in fecha.partidos.all():
            jugar(p, 1, 0)

    def test_par_hasta_la_final(self):
        camp = crear_campeonato(8)
        [fecha] = fixture.generar_fixture(camp, modalidad='RELAMPAGO', semilla=3)
        self.assertEqual((fecha.fase, fecha.partidos.count()), ('CUARTOS', 4))
        fases = [fecha.fase]
        while True:
            self.jugar_ronda(fecha)
            fecha = avanzar_eliminatoria(camp)
            if fecha is None:
                break
            fases.append(fecha.fase)
        self.assertEqual(fases, ['CUARTOS', 'SEMIFINAL', 'FINAL'])
        camp.refresh_from_db()
        self.assertEqual((camp.estado, camp.modalidad), ('FINALIZADO', 'RELAMPAGO'))

    def test_impar_pide_confirmar_y_el_descanso_avanza(self):
        camp = crear_campeonato(5)
        with self.assertRaises(fixture.RequiereConfirmacion):
            fixture.generar_fixture(camp, modalidad='RELAMPAGO')
        self.assertEqual(camp.partidos.count(), 0)  # nada quedó a medias
        [fecha] = fixture.generar_fixture(camp, modalidad='RELAMPAGO', confirmar_descanso=True, semilla=1)
        self.assertEqual((fecha.partidos.count(), fecha.descansos.count()), (2, 1))
        libre = fecha.descansos.get().equipo
        self.jugar_ronda(fecha)
        with self.assertRaises(fixture.RequiereConfirmacion):  # avanzan 3: otro descansa
            avanzar_eliminatoria(camp)
        ronda2 = avanzar_eliminatoria(camp, confirmar_descanso=True, semilla=1)
        self.assertEqual((ronda2.partidos.count(), ronda2.descansos.count()), (1, 1))
        self.assertNotEqual(ronda2.descansos.get().equipo, libre)  # no repite descanso
        self.jugar_ronda(ronda2)
        final = avanzar_eliminatoria(camp)  # 2 avanzan: ganador + el que descansó
        self.assertEqual((final.fase, final.partidos.count()), ('FINAL', 1))

    def test_relampago_no_aplica_a_grupos(self):
        camp = crear_campeonato(4, formato='GRUPOS', num_grupos=2)
        fixture.sortear_grupos(camp)
        with self.assertRaises(fixture.FixtureError):
            fixture.generar_fixture(camp, modalidad='RELAMPAGO')

    def test_empate_sin_penales_no_avanza(self):
        camp = crear_campeonato(2)
        [fecha] = fixture.generar_fixture(camp, modalidad='RELAMPAGO')
        jugar(fecha.partidos.get(), 1, 1)
        with self.assertRaises(fixture.FixtureError):
            avanzar_eliminatoria(camp)

    def test_modalidades_ida_y_ida_vuelta(self):
        solo_ida = crear_campeonato(4, 'Ida')
        fixture.generar_fixture(solo_ida, modalidad='IDA')
        self.assertEqual((solo_ida.partidos.count(), solo_ida.modalidad, solo_ida.ida_vuelta), (6, 'IDA', False))
        doble = crear_campeonato(4, 'Doble')
        fixture.generar_fixture(doble, modalidad='IDA_VUELTA')
        self.assertEqual((doble.partidos.count(), doble.modalidad, doble.ida_vuelta), (12, 'IDA_VUELTA', True))
        with self.assertRaises(fixture.FixtureError):
            fixture.generar_fixture(crear_campeonato(4, 'Otra'), modalidad='otra')


class ProgramacionTests(APITestCase):
    def dias(self, camp):
        """[(fecha, [horas de sus partidos])] en hora local."""
        from django.utils import timezone
        res = []
        for f in camp.fechas.order_by('numero'):
            locales = [timezone.localtime(p.fecha_hora) for p in f.partidos.order_by('fecha_hora') if p.fecha_hora]
            res.append((f.fecha_programada, [d.strftime('%a %Y-%m-%d %H:%M') for d in locales]))
        return res

    def test_martes_y_jueves_con_dos_partidos_por_fecha(self):
        from datetime import date
        camp = crear_campeonato(4)
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4],
                                hora_inicio='19:00', minutos_entre_partidos=90, semilla=1)
        d = self.dias(camp)
        self.assertEqual([x[0].isoweekday() for x in d], [2, 4, 2])  # mar, jue, mar
        self.assertEqual([str(x[0]) for x in d], ['2026-10-13', '2026-10-15', '2026-10-20'])
        # los dos partidos de cada fecha el mismo día, uno tras otro
        self.assertEqual(d[0][1], ['Tue 2026-10-13 19:00', 'Tue 2026-10-13 20:30'])
        self.assertEqual(d[1][1], ['Thu 2026-10-15 19:00', 'Thu 2026-10-15 20:30'])

    def test_inicio_en_dia_no_permitido_salta_al_siguiente(self):
        from datetime import date
        camp = crear_campeonato(4)
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 14), dias_semana=[2, 4])  # miércoles
        self.assertEqual(str(camp.fechas.get(numero=1).fecha_programada), '2026-10-15')  # jueves

    def test_partidos_por_dia_reparte_una_fecha_en_varios_dias(self):
        from datetime import date
        camp = crear_campeonato(4)
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4], partidos_por_dia=1)
        dias = sorted({str(p.fecha_hora.date()) for p in camp.fechas.get(numero=1).partidos.all()} |
                      {str(p.fecha_hora.date()) for p in camp.fechas.get(numero=2).partidos.all()})
        self.assertEqual(len(dias), 4)  # 4 partidos, cada uno en su día
        self.assertEqual(str(camp.fechas.get(numero=2).fecha_programada), '2026-10-20')

    def test_sin_dias_usa_intervalo_y_sin_inicio_no_programa(self):
        from datetime import date
        camp = crear_campeonato(4, 'Int')
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_entre_fechas=3)
        self.assertEqual([str(f.fecha_programada) for f in camp.fechas.order_by('numero')],
                         ['2026-10-13', '2026-10-16', '2026-10-19'])
        sin = crear_campeonato(4, 'Sin')
        fixture.generar_fixture(sin)
        self.assertFalse(sin.partidos.filter(fecha_hora__isnull=False).exists())

    def test_primera_ronda_se_programa_y_las_siguientes_se_eligen_a_mano(self):
        from datetime import date
        camp = crear_campeonato(4, 'Rel')
        [ronda1] = fixture.generar_fixture(camp, modalidad='RELAMPAGO', fecha_inicio=date(2026, 10, 13),
                                           dias_semana=[2, 4])
        for p in ronda1.partidos.all():
            jugar(p, 1, 0)
        final = avanzar_eliminatoria(camp)
        ronda1.refresh_from_db()
        self.assertEqual(str(ronda1.fecha_programada), '2026-10-13')
        # la final se crea sin día ni hora: se programa cuando el administrador la elige
        self.assertIsNone(final.fecha_programada)
        self.assertFalse(final.partidos.filter(fecha_hora__isnull=False).exists())

    def test_valida_dias_invalidos(self):
        camp = crear_campeonato(4, 'Val')
        with self.assertRaises(fixture.FixtureError):
            fixture.generar_fixture(camp, dias_semana=[0, 9])


class PostergacionYTarjetasTests(APITestCase):
    def setUp(self):
        from datetime import date
        User = get_user_model()
        self.op = User.objects.create_user('op2', password='clave-larga-1')
        self.op.groups.add(Group.objects.create(name='Operador'))
        self.camp = crear_campeonato(4, publicado=True)
        fixture.generar_fixture(self.camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2])
        self.p = self.camp.partidos.first()
        self.url = f'/api/v1/partidos/{self.p.id}/cambiar-estado/'

    def test_postergar_exige_motivo(self):
        self.client.force_authenticate(self.op)
        r = self.client.post(self.url, {'estado': 'POSTERGADO'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('motivo_postergacion', r.data)
        self.p.refresh_from_db()
        self.assertEqual(self.p.estado, 'PENDIENTE')

    def test_postergar_con_nueva_fecha_guarda_original_y_notifica(self):
        self.client.force_authenticate(self.op)
        original = self.p.fecha_hora
        r = self.client.post(self.url, {'estado': 'POSTERGADO', 'motivo': 'Lluvia intensa',
                                        'fecha_hora': '2026-10-22T19:00:00-05:00'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.p.refresh_from_db()
        self.assertEqual((self.p.estado, self.p.motivo_postergacion), ('POSTERGADO', 'Lluvia intensa'))
        self.assertEqual(self.p.fecha_original, original)
        self.assertEqual(self.p.fecha_hora.isoformat(), '2026-10-23T00:00:00+00:00')
        noti = Notificacion.objects.get(partido=self.p)
        self.assertIn('Lluvia intensa', noti.cuerpo)
        self.assertIn('Nueva fecha: 22/10 19:00', noti.cuerpo)

    def test_postergar_sin_fecha_queda_por_definir(self):
        self.client.force_authenticate(self.op)
        original = self.p.fecha_hora
        self.client.post(self.url, {'estado': 'POSTERGADO', 'motivo': 'Cancha no disponible'}, format='json')
        self.p.refresh_from_db()
        self.assertIsNone(self.p.fecha_hora)
        self.assertEqual(self.p.fecha_original, original)
        self.assertIn('por definir', Notificacion.objects.get(partido=self.p).cuerpo)

    def test_el_publico_ve_motivo_y_fechas(self):
        self.client.force_authenticate(self.op)
        self.client.post(self.url, {'estado': 'POSTERGADO', 'motivo': 'Lluvia', 'fecha_hora': '2026-10-22T19:00:00-05:00'},
                         format='json')
        self.client.force_authenticate(None)
        d = self.client.get(f'/api/v1/partidos/{self.p.id}/').data
        self.assertEqual((d['estado'], d['motivo_postergacion']), ('POSTERGADO', 'Lluvia'))
        self.assertTrue(d['fecha_original'] and d['fecha_hora'])

    def test_tarjetas_por_partido_y_ranking(self):
        j1 = Jugador.objects.create(equipo=self.p.local, nombres='Ana', apellidos='Lopez', numero=5)
        j2 = Jugador.objects.create(equipo=self.p.visitante, nombres='Beto', apellidos='Mora', numero=8)
        for tipo, eq, j in [('AMARILLA', self.p.local, j1), ('AMARILLA', self.p.local, j1), ('DOBLE_AMARILLA', self.p.visitante, j2),
                            ('AMARILLA', self.p.visitante, j2), ('INCIDENCIA', self.p.local, None)]:
            EventoPartido.objects.create(partido=self.p, tipo=tipo, equipo=eq, jugador=j, minuto=10)
        d = self.client.get(f'/api/v1/partidos/{self.p.id}/').data
        self.assertEqual(d['tarjetas'], {'local': {'amarillas': 2, 'rojas': 0}, 'visitante': {'amarillas': 3, 'rojas': 1}})
        rank = self.client.get(f'/api/v1/campeonatos/{self.camp.id}/tarjetas/').data
        self.assertEqual([(r['nombre'], r['amarillas'], r['rojas']) for r in rank],
                         [('Beto Mora', 3, 1), ('Ana Lopez', 2, 0)])

    def test_goleadores_por_campeonato(self):
        j1 = Jugador.objects.create(equipo=self.p.local, nombres='Ana', apellidos='Lopez', numero=5)
        j2 = Jugador.objects.create(equipo=self.p.visitante, nombres='Beto', apellidos='Mora', numero=8)
        for j, eq in [(j1, self.p.local), (j1, self.p.local), (j2, self.p.visitante)]:
            EventoPartido.objects.create(partido=self.p, tipo='GOL', equipo=eq, jugador=j, minuto=5)
        EventoPartido.objects.create(partido=self.p, tipo='AUTOGOL', equipo=self.p.local, jugador=j1, minuto=9)
        g = self.client.get(f'/api/v1/campeonatos/{self.camp.id}/goleadores/').data
        self.assertEqual([(x['nombre'], x['goles']) for x in g], [('Ana Lopez', 2), ('Beto Mora', 1)])  # sin autogoles


class PrimeraFechaManualTests(APITestCase):
    def equipos(self, camp):
        return [i.equipo for i in camp.inscripciones.order_by('equipo__nombre')]

    def pares(self, fecha):
        return {(p.local_id, p.visitante_id) for p in fecha.partidos.all()}

    def test_par_primera_fecha_fija_y_resto_completa_el_todos_contra_todos(self):
        camp = crear_campeonato(6, 'Man')
        e = self.equipos(camp)
        elegidos = [(e[3], e[0]), (e[1], e[5]), (e[2], e[4])]  # local, visitante a mano
        fixture.generar_fixture(camp, semilla=7, primera_fecha=[{'local': a.id, 'visitante': b.id} for a, b in elegidos])
        self.assertEqual(self.pares(camp.fechas.get(numero=1)), {(a.id, b.id) for a, b in elegidos})
        self.assertEqual(camp.partidos.count(), 15)
        cruces = {frozenset((p.local_id, p.visitante_id)) for p in camp.partidos.all()}
        self.assertEqual(len(cruces), 15)  # ningún cruce repetido
        for f in camp.fechas.all():
            ids = [x for p in f.partidos.all() for x in (p.local_id, p.visitante_id)]
            self.assertEqual(len(ids), len(set(ids)))

    def test_impar_el_equipo_sin_partido_descansa_la_fecha_1(self):
        camp = crear_campeonato(5, 'Imp')
        e = self.equipos(camp)
        fixture.generar_fixture(camp, confirmar_descanso=True, semilla=2,
                                primera_fecha=[{'local': e[0].id, 'visitante': e[1].id},
                                               {'local': e[2].id, 'visitante': e[3].id}])
        f1 = camp.fechas.get(numero=1)
        self.assertEqual(f1.descansos.get().equipo_id, e[4].id)
        self.assertEqual(camp.partidos.count(), 10)
        descansos = [d.equipo_id for f in camp.fechas.all() for d in f.descansos.all()]
        self.assertEqual(sorted(descansos), sorted(x.id for x in e))  # cada uno descansa una vez

    def test_ida_y_vuelta_refleja_la_primera_fecha(self):
        camp = crear_campeonato(4, 'IV')
        e = self.equipos(camp)
        fixture.generar_fixture(camp, modalidad='IDA_VUELTA', semilla=1,
                                primera_fecha=[{'local': e[1].id, 'visitante': e[0].id},
                                               {'local': e[3].id, 'visitante': e[2].id}])
        self.assertEqual(self.pares(camp.fechas.get(numero=1)), {(e[1].id, e[0].id), (e[3].id, e[2].id)})
        self.assertEqual(self.pares(camp.fechas.get(numero=4)), {(e[0].id, e[1].id), (e[2].id, e[3].id)})

    def test_validaciones(self):
        camp = crear_campeonato(4, 'Val2')
        e = self.equipos(camp)
        otro = Equipo.objects.create(nombre='Ajeno')
        malos = [
            [{'local': e[0].id, 'visitante': e[1].id}],                                   # incompleta
            [{'local': e[0].id, 'visitante': e[1].id}, {'local': e[1].id, 'visitante': e[2].id}],  # repetido
            [{'local': e[0].id, 'visitante': e[0].id}, {'local': e[2].id, 'visitante': e[3].id}],  # contra sí mismo
            [{'local': e[0].id, 'visitante': otro.id}, {'local': e[2].id, 'visitante': e[3].id}],  # no inscrito
            [{'local': 'x'}],                                                              # formato
        ]
        for primera in malos:
            with self.assertRaises(fixture.FixtureError, msg=str(primera)):
                fixture.generar_fixture(camp, primera_fecha=primera)
        self.assertEqual(camp.partidos.count(), 0)

    def test_no_aplica_a_grupos(self):
        camp = crear_campeonato(4, 'Grp', formato='GRUPOS', num_grupos=2)
        fixture.sortear_grupos(camp)
        e = self.equipos(camp)
        with self.assertRaises(fixture.FixtureError):
            fixture.generar_fixture(camp, primera_fecha=[{'local': e[0].id, 'visitante': e[1].id},
                                                         {'local': e[2].id, 'visitante': e[3].id}])

    def test_relampago_con_cruces_elegidos(self):
        camp = crear_campeonato(4, 'Rel2')
        e = self.equipos(camp)
        [f] = fixture.generar_fixture(camp, modalidad='RELAMPAGO',
                                      primera_fecha=[{'local': e[0].id, 'visitante': e[3].id},
                                                     {'local': e[2].id, 'visitante': e[1].id}])
        self.assertEqual(self.pares(f), {(e[0].id, e[3].id), (e[2].id, e[1].id)})

    def test_api_acepta_primera_fecha(self):
        admin = get_user_model().objects.create_superuser('rootm', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(4, 'Api', publicado=True)
        e = self.equipos(camp)
        url = f'/api/v1/campeonatos/{camp.id}/generar-fixture/'
        mala = self.client.post(url, {'primera_fecha': [{'local': e[0].id, 'visitante': e[1].id}]}, format='json')
        self.assertEqual(mala.status_code, 400)
        ok = self.client.post(url, {'primera_fecha': [{'local': e[0].id, 'visitante': e[1].id},
                                                      {'local': e[2].id, 'visitante': e[3].id}]}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)


class ReglaDelSorteoTests(APITestCase):
    def test_se_puede_repetir_mientras_nada_haya_comenzado(self):
        camp = crear_campeonato(4, 'Re')
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        p.estado, p.motivo_postergacion = 'POSTERGADO', 'Lluvia'  # postergado: no se jugó
        p.save()
        self.assertIsNone(fixture.sorteo_bloqueado(camp))
        fixture.generar_fixture(camp, regenerar=True)
        self.assertEqual(camp.partidos.count(), 6)

    def test_un_partido_iniciado_bloquea(self):
        camp = crear_campeonato(4, 'Bloq')
        fixture.generar_fixture(camp)
        for estado in ('INICIADO', 'ENTRETIEMPO', 'FINALIZADO'):
            p = camp.partidos.first()
            p.estado = estado
            p.save()
            self.assertTrue(fixture.sorteo_bloqueado(camp), estado)
            with self.assertRaises(fixture.FixtureError):
                fixture.generar_fixture(camp, regenerar=True)
        p.estado = 'PENDIENTE'
        p.save()
        self.assertIsNone(fixture.sorteo_bloqueado(camp))

    def test_un_evento_registrado_bloquea(self):
        camp = crear_campeonato(4, 'Ev')
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        EventoPartido.objects.create(partido=p, tipo='AMARILLA', equipo=p.local, minuto=5)
        self.assertTrue(fixture.sorteo_bloqueado(camp))

    def test_resortear_grupos_descarta_el_fixture_si_nada_comenzo(self):
        camp = crear_campeonato(8, 'Gr', formato='GRUPOS', num_grupos=2)
        fixture.sortear_grupos(camp)
        fixture.generar_fixture(camp)
        fixture.sortear_grupos(camp)  # permitido: se descarta el fixture
        self.assertEqual(camp.partidos.count(), 0)
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        p.estado = 'FINALIZADO'
        p.save()
        with self.assertRaises(fixture.FixtureError):
            fixture.sortear_grupos(camp)

    def test_fixture_info_informa_el_bloqueo(self):
        admin = get_user_model().objects.create_superuser('rootb', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(4, 'Info', publicado=True)
        fixture.generar_fixture(camp)
        url = f'/api/v1/campeonatos/{camp.id}/fixture-info/'
        self.assertIsNone(self.client.get(url).data['sorteo_bloqueado'])
        p = camp.partidos.first()
        p.estado = 'INICIADO'
        p.save()
        self.assertIn('no se puede repetir', self.client.get(url).data['sorteo_bloqueado'])


class FeriadosTests(APITestCase):
    def fechas(self, camp):
        return [str(f.fecha_programada) for f in camp.fechas.order_by('numero')]

    def test_sin_decision_pregunta_y_no_cambia_nada(self):
        from datetime import date
        from torneos.models import Feriado
        from torneos.services.programacion import ConflictoFeriados
        camp = crear_campeonato(4, 'Fer')
        Feriado.objects.create(fecha=date(2026, 10, 15), nombre='Feriado local')
        with self.assertRaises(ConflictoFeriados) as cm:
            fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4])
        self.assertEqual(cm.exception.como_json(), [{'fecha': '2026-10-15', 'nombre': 'Feriado local'}])
        self.assertEqual((camp.partidos.count(), camp.fechas.count()), (0, 0))  # transacción revertida

    def test_omitir_salta_el_feriado_y_jugar_lo_respeta(self):
        from datetime import date
        from torneos.models import Feriado
        Feriado.objects.create(fecha=date(2026, 10, 15), nombre='Feriado local')
        omitir = crear_campeonato(4, 'Om')
        fixture.generar_fixture(omitir, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4], feriados='omitir')
        self.assertEqual(self.fechas(omitir), ['2026-10-13', '2026-10-20', '2026-10-22'])  # el jueves 15 se salta
        jugar_igual = crear_campeonato(4, 'Ju')
        fixture.generar_fixture(jugar_igual, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4], feriados='jugar')
        self.assertEqual(self.fechas(jugar_igual), ['2026-10-13', '2026-10-15', '2026-10-20'])

    def test_sin_conflicto_no_pregunta(self):
        from datetime import date
        from torneos.models import Feriado
        Feriado.objects.create(fecha=date(2026, 12, 25), nombre='Navidad')  # lejos del calendario
        camp = crear_campeonato(4, 'SinCon')
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4])
        self.assertEqual(self.fechas(camp), ['2026-10-13', '2026-10-15', '2026-10-20'])

    def test_fecha_de_inicio_en_feriado_con_omitir(self):
        from datetime import date
        from torneos.models import Feriado
        Feriado.objects.create(fecha=date(2026, 10, 13), nombre='Feriado')
        camp = crear_campeonato(4, 'Ini')
        fixture.generar_fixture(camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4], feriados='omitir')
        self.assertEqual(self.fechas(camp)[0], '2026-10-15')

    def test_feriado_anual_y_alcance_por_campeonato(self):
        from datetime import date
        from torneos.models import Feriado
        from torneos.services.feriados import CalendarioFeriados
        a, b = crear_campeonato(4, 'A'), crear_campeonato(4, 'B')
        Feriado.objects.create(fecha=date(2020, 5, 1), nombre='Día del Trabajo', se_repite=True)
        Feriado.objects.create(fecha=date(2026, 7, 4), nombre='Solo de A', campeonato=a)
        Feriado.objects.create(fecha=date(2026, 8, 10), nombre='Una vez')
        self.assertEqual(CalendarioFeriados(a).nombre(date(2031, 5, 1)), 'Día del Trabajo')  # cada año
        self.assertEqual(CalendarioFeriados(a).nombre(date(2026, 7, 4)), 'Solo de A')
        self.assertIsNone(CalendarioFeriados(b).nombre(date(2026, 7, 4)))                       # no afecta a B
        self.assertIsNone(CalendarioFeriados(a).nombre(date(2027, 8, 10)))                      # no se repite

    def test_crud_solo_administrador_y_lectura_publica(self):
        User = get_user_model()
        admin = User.objects.create_superuser('rootf', password='clave-larga-1')
        op = User.objects.create_user('opf', password='clave-larga-1')
        op.groups.add(Group.objects.create(name='Operador'))
        cuerpo = {'fecha': '2026-11-02', 'nombre': 'Día de Difuntos', 'se_repite': True}
        self.assertEqual(self.client.post('/api/v1/feriados/', cuerpo, format='json').status_code, 401)
        self.client.force_authenticate(op)
        self.assertEqual(self.client.post('/api/v1/feriados/', cuerpo, format='json').status_code, 403)
        self.client.force_authenticate(admin)
        r = self.client.post('/api/v1/feriados/', cuerpo, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.client.post('/api/v1/feriados/', cuerpo, format='json').status_code, 400)  # repetido
        self.assertEqual(self.client.patch(f'/api/v1/feriados/{r.data["id"]}/', {'nombre': 'Difuntos'},
                                           format='json').status_code, 200)
        self.client.force_authenticate(None)
        self.assertEqual([f['nombre'] for f in self.client.get('/api/v1/feriados/').data['results']], ['Difuntos'])
        self.client.force_authenticate(admin)
        self.assertEqual(self.client.delete(f'/api/v1/feriados/{r.data["id"]}/').status_code, 204)

    def test_api_generar_fixture_pregunta_por_feriados(self):
        from datetime import date
        from torneos.models import Feriado
        admin = get_user_model().objects.create_superuser('rootg', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(4, 'ApiF', publicado=True)
        Feriado.objects.create(fecha=date(2026, 10, 15), nombre='Feriado local')
        url = f'/api/v1/campeonatos/{camp.id}/generar-fixture/'
        base = {'fecha_inicio': '2026-10-13', 'dias_semana': [2, 4]}
        r = self.client.post(url, base, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.data['requiere_decision_feriados'])
        self.assertEqual(r.data['feriados'], [{'fecha': '2026-10-15', 'nombre': 'Feriado local'}])
        self.assertEqual(self.client.post(url, {**base, 'feriados': 'quizas'}, format='json').status_code, 400)
        ok = self.client.post(url, {**base, 'feriados': 'omitir'}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(self.fechas(camp), ['2026-10-13', '2026-10-20', '2026-10-22'])

    def test_programar_ronda_a_mano_avisa_si_es_feriado(self):
        from datetime import date
        from torneos.models import Feriado
        admin = get_user_model().objects.create_superuser('roott', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(2, 'Fin', publicado=True)
        [final] = fixture.generar_fixture(camp, modalidad='RELAMPAGO')
        p = final.partidos.get()
        Feriado.objects.create(fecha=date(2026, 11, 3), nombre='Feriado')
        url = f'/api/v1/fechas/{final.id}/programar/'
        cuerpo = {'partidos': [{'id': p.id, 'fecha_hora': '2026-11-03T19:00:00-05:00'}]}
        r = self.client.post(url, cuerpo, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.data['requiere_confirmacion_feriados'])
        self.assertEqual(r.data['feriados'][0]['nombre'], 'Feriado')
        p.refresh_from_db()
        self.assertIsNone(p.fecha_hora)  # nada se guardó
        ok = self.client.post(url, {**cuerpo, 'confirmar_feriados': True}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        p.refresh_from_db()
        self.assertIsNotNone(p.fecha_hora)


class HorariosFijosTests(APITestCase):
    """«BODEGA siempre en el 2.º horario» (20:30): sólo se intercambian horas dentro del mismo día."""

    def setUp(self):
        from datetime import date
        self.camp = crear_campeonato(6, 'Hor', publicado=True)           # 3 partidos por fecha
        self.equipos = list(self.camp.inscripciones.order_by('equipo__nombre'))
        self.bodega = self.equipos[0].equipo
        fixture.generar_fixture(self.camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2], semilla=5)
        self.admin = get_user_model().objects.create_superuser('rooth', password='clave-larga-1')

    def foto(self):
        """{fecha: {(local, visitante): hora}} para comparar antes/después."""
        from django.utils import timezone
        return {f.numero: {(p.local_id, p.visitante_id): timezone.localtime(p.fecha_hora).strftime('%H:%M')
                           for p in f.partidos.all()} for f in self.camp.fechas.order_by('numero')}

    def segundo(self, fecha):
        from django.utils import timezone
        orden = sorted(fecha.partidos.all(), key=lambda p: timezone.localtime(p.fecha_hora))
        return orden[1]

    def test_el_equipo_queda_siempre_en_el_segundo_horario_sin_tocar_los_cruces(self):
        from torneos.services.horarios import aplicar_horarios_fijos
        antes = self.foto()
        cambios, sin = aplicar_horarios_fijos(self.camp, [{'equipo': self.bodega.id, 'orden': 2}])
        despues = self.foto()
        self.assertEqual(sin, [])
        for numero, partidos in despues.items():
            self.assertEqual(set(partidos), set(antes[numero]))                          # mismos cruces por fecha
            self.assertEqual(sorted(partidos.values()), sorted(antes[numero].values()))  # mismas horas por fecha
        for f in self.camp.fechas.all():
            if f.partidos.filter(local=self.bodega).exists() or f.partidos.filter(visitante=self.bodega).exists():
                self.assertIn(self.bodega.id, (self.segundo(f).local_id, self.segundo(f).visitante_id))
        self.assertTrue(cambios)

    def test_idempotente_y_la_simulacion_no_guarda(self):
        from torneos.services.horarios import aplicar_horarios_fijos
        reglas = [{'equipo': self.bodega.id, 'orden': 2}]
        antes = self.foto()
        aplicar_horarios_fijos(self.camp, reglas, simular=True)
        self.assertEqual(self.foto(), antes)
        aplicar_horarios_fijos(self.camp, reglas)
        self.assertEqual(aplicar_horarios_fijos(self.camp, reglas), ([], []))            # ya está: no hay más cambios

    def test_no_toca_dias_con_partidos_iniciados(self):
        from torneos.services.horarios import aplicar_horarios_fijos
        for f in self.camp.fechas.order_by('numero'):
            partido = next((p for p in f.partidos.all() if self.bodega.id in (p.local_id, p.visitante_id)), None)
            if partido:
                otro = next(p for p in f.partidos.all() if p.id != partido.id)
                otro.estado = 'FINALIZADO'
                otro.save()
        antes = self.foto()
        aplicar_horarios_fijos(self.camp, [{'equipo': self.bodega.id, 'orden': 2}])
        self.assertEqual(self.foto(), antes)

    def test_dos_reglas_que_piden_el_mismo_horario_se_avisan(self):
        from torneos.services.horarios import aplicar_horarios_fijos
        otro = self.equipos[1].equipo
        cambios, sin = aplicar_horarios_fijos(self.camp, [{'equipo': self.bodega.id, 'orden': 2},
                                                           {'equipo': otro.id, 'orden': 2}])
        # cuando los dos juegan el mismo día, la segunda regla no puede quitarle el horario a la primera
        self.assertTrue(any('ya ocupa ese horario' in s['motivo'] for s in sin))

    def test_horario_mayor_a_los_partidos_del_dia(self):
        from torneos.services.horarios import aplicar_horarios_fijos
        _, sin = aplicar_horarios_fijos(self.camp, [{'equipo': self.bodega.id, 'orden': 5}])
        self.assertTrue(sin and 'solo hay' in sin[0]['motivo'])

    def test_se_aplica_solo_al_repetir_el_sorteo(self):
        from datetime import date
        self.client.force_authenticate(self.admin)
        url = f'/api/v1/campeonatos/{self.camp.id}/horarios-fijos/'
        self.client.post(url, {'reglas': [{'equipo': self.bodega.id, 'orden': 2}], 'confirmar': True}, format='json')
        self.camp.refresh_from_db()  # la API guardó las reglas en otra instancia
        fixture.generar_fixture(self.camp, regenerar=True, fecha_inicio=date(2026, 10, 13), dias_semana=[2], semilla=11)
        jugados = 0
        for f in self.camp.fechas.all():
            if any(self.bodega.id in (p.local_id, p.visitante_id) for p in f.partidos.all()):
                jugados += 1
                self.assertIn(self.bodega.id, (self.segundo(f).local_id, self.segundo(f).visitante_id))
        self.assertGreater(jugados, 0)

    def test_api_simula_guarda_y_valida(self):
        User = get_user_model()
        op = User.objects.create_user('oph', password='clave-larga-1')
        op.groups.add(Group.objects.create(name='Operador'))
        url = f'/api/v1/campeonatos/{self.camp.id}/horarios-fijos/'
        regla = [{'equipo': self.bodega.id, 'orden': 2}]
        self.client.force_authenticate(op)
        self.assertEqual(self.client.post(url, {'reglas': regla}, format='json').status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.post(url, {'reglas': regla}, format='json')
        self.assertEqual((r.status_code, r.data['aplicado']), (200, False))
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.horarios_fijos, [])                         # simular no guarda las reglas
        r = self.client.post(url, {'reglas': regla, 'confirmar': True}, format='json')
        self.assertTrue(r.data['aplicado'])
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.horarios_fijos, regla)
        ajeno = Equipo.objects.create(nombre='No inscrito')
        for malo in ([{'equipo': ajeno.id, 'orden': 2}], [{'equipo': self.bodega.id, 'orden': 0}],
                     [{'equipo': self.bodega.id, 'orden': 2}, {'equipo': self.bodega.id, 'orden': 1}], 'x', [{'a': 1}]):
            self.assertEqual(self.client.post(url, {'reglas': malo}, format='json').status_code, 400, malo)
        self.client.post(url, {'reglas': [], 'confirmar': True}, format='json')   # vaciar quita la regla
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.horarios_fijos, [])

    def test_las_reglas_no_se_editan_por_el_crud(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(f'/api/v1/campeonatos/{self.camp.id}/', {'horarios_fijos': [{'equipo': 1, 'orden': 2}]}, format='json')
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.horarios_fijos, [])


class AplicarFeriadosTests(APITestCase):
    def setUp(self):
        from datetime import date
        self.camp = crear_campeonato(4, 'Apl', publicado=True)
        fixture.generar_fixture(self.camp, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4])  # 13, 15, 20
        self.admin = get_user_model().objects.create_superuser('roota', password='clave-larga-1')

    def dias(self):
        return [str(f.fecha_programada) for f in self.camp.fechas.order_by('numero')]

    def horas(self, fecha):
        from django.utils import timezone
        return [timezone.localtime(p.fecha_hora).strftime('%a %d %H:%M')
                for p in fecha.partidos.order_by('fecha_hora')]

    def feriado(self, dia, **kw):
        from torneos.models import Feriado
        return Feriado.objects.create(fecha=dia, nombre='Feriado local', **kw)

    def test_simular_no_cambia_nada_y_aplicar_corre_en_cadena(self):
        from datetime import date
        from torneos.services.programacion import aplicar_feriados
        self.feriado(date(2026, 10, 15))
        cambios, sin_mover = aplicar_feriados(self.camp, simular=True)
        self.assertEqual([(c['nombre'], c['desde'], c['hasta']) for c in cambios],
                         [('Fecha 2', '2026-10-15', '2026-10-20'), ('Fecha 3', '2026-10-20', '2026-10-22')])
        self.assertEqual(cambios[0]['feriado'], 'Feriado local')
        self.assertEqual(sin_mover, [])
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-15', '2026-10-20'])  # simulación: igual
        aplicar_feriados(self.camp, simular=False)
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-20', '2026-10-22'])
        self.assertEqual(self.horas(self.camp.fechas.get(numero=2)), ['Tue 20 19:00', 'Tue 20 20:30'])  # mantiene la hora
        self.assertFalse(aplicar_feriados(self.camp, simular=True)[0])  # ya no queda nada por mover

    def test_conserva_la_hora_editada_a_mano(self):
        from datetime import date, datetime
        from django.utils import timezone
        from torneos.services.programacion import aplicar_feriados
        self.feriado(date(2026, 10, 15))
        p = self.camp.fechas.get(numero=2).partidos.order_by('fecha_hora').first()
        p.fecha_hora = timezone.make_aware(datetime(2026, 10, 15, 21, 15))
        p.save()
        aplicar_feriados(self.camp, simular=False)
        p.refresh_from_db()
        self.assertEqual(timezone.localtime(p.fecha_hora).strftime('%Y-%m-%d %H:%M'), '2026-10-20 21:15')

    def test_no_toca_fechas_jugadas_ni_con_partidos_postergados(self):
        from datetime import date
        from torneos.services.programacion import aplicar_feriados
        for p in self.camp.fechas.get(numero=1).partidos.all():
            jugar(p, 1, 0)
        self.feriado(date(2026, 10, 13))  # cae en la fecha 1, que ya se jugó
        self.assertEqual(aplicar_feriados(self.camp, simular=False)[0], [])
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-15', '2026-10-20'])
        p = self.camp.fechas.get(numero=2).partidos.first()
        p.estado, p.motivo_postergacion = 'POSTERGADO', 'Lluvia'
        p.save()
        self.feriado(date(2026, 10, 15))
        cambios, sin_mover = aplicar_feriados(self.camp, simular=False)
        self.assertEqual(cambios, [])  # la fecha 2 tiene un partido postergado: no se mueve sola
        self.assertEqual([s['dia'] for s in sin_mover], ['2026-10-15'])  # pero se avisa del partido pendiente en feriado
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-15', '2026-10-20'])

    def test_feriado_anual_y_de_otro_campeonato(self):
        from datetime import date
        from torneos.services.programacion import aplicar_feriados
        otro = crear_campeonato(4, 'Otro')
        self.feriado(date(2020, 10, 15), campeonato=otro)       # no es de este campeonato
        self.assertEqual(aplicar_feriados(self.camp)[0], [])
        self.feriado(date(2020, 10, 15), se_repite=True)        # global y anual: sí aplica en 2026
        self.assertEqual(len(aplicar_feriados(self.camp)[0]), 2)

    def test_las_rondas_programadas_a_mano_solo_se_avisan(self):
        from datetime import date
        from torneos.services.programacion import aplicar_feriados
        camp = crear_campeonato(4, 'Rel9', publicado=True)
        [r1] = fixture.generar_fixture(camp, modalidad='RELAMPAGO', fecha_inicio=date(2026, 10, 13), dias_semana=[2])
        for p in r1.partidos.all():
            jugar(p, 1, 0)
        final = avanzar_eliminatoria(camp)
        partido = final.partidos.get()
        from django.utils import timezone
        from datetime import datetime
        partido.fecha_hora = timezone.make_aware(datetime(2026, 11, 5, 19, 0))
        partido.save()
        self.feriado(date(2026, 11, 5))
        cambios, sin_mover = aplicar_feriados(camp, simular=False)
        self.assertEqual(cambios, [])
        self.assertEqual([(s['dia'], s['feriado']) for s in sin_mover], [('2026-11-05', 'Feriado local')])
        partido.refresh_from_db()
        self.assertEqual(timezone.localtime(partido.fecha_hora).day, 5)  # no se movió

    def test_api_simula_y_aplica_solo_el_administrador(self):
        from datetime import date
        User = get_user_model()
        op = User.objects.create_user('opa', password='clave-larga-1')
        op.groups.add(Group.objects.create(name='Operador'))
        self.feriado(date(2026, 10, 15))
        url = f'/api/v1/campeonatos/{self.camp.id}/aplicar-feriados/'
        self.client.force_authenticate(op)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.post(url, {}, format='json')
        self.assertEqual((r.status_code, r.data['aplicado'], len(r.data['cambios'])), (200, False, 2))
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-15', '2026-10-20'])
        r = self.client.post(url, {'confirmar': True}, format='json')
        self.assertEqual((r.data['aplicado'], len(r.data['cambios'])), (True, 2))
        self.assertEqual(self.dias(), ['2026-10-13', '2026-10-20', '2026-10-22'])


class LimpiarSorteoTests(APITestCase):
    def test_limpia_todo_y_conserva_los_equipos(self):
        camp = crear_campeonato(8, 'Lim', formato='GRUPOS', num_grupos=2)
        fixture.sortear_grupos(camp)
        fixture.generar_fixture(camp)
        resumen = fixture.limpiar_sorteo(camp)
        self.assertEqual(resumen, {'partidos': 12, 'fechas': 3, 'grupos': 2})
        camp.refresh_from_db()
        self.assertEqual((camp.partidos.count(), camp.fechas.count(), camp.grupos.count()), (0, 0, 0))
        self.assertEqual(camp.estado, 'BORRADOR')
        self.assertEqual(camp.inscripciones.count(), 8)
        self.assertFalse(camp.inscripciones.filter(grupo__isnull=False).exists())
        fixture.sortear_grupos(camp)  # se puede empezar de nuevo
        fixture.generar_fixture(camp)
        self.assertEqual(camp.partidos.count(), 12)

    def test_limpia_relampago_con_descansos_y_vuelve_a_borrador(self):
        camp = crear_campeonato(5, 'LimRel')
        fixture.generar_fixture(camp, modalidad='RELAMPAGO', confirmar_descanso=True)
        fixture.limpiar_sorteo(camp)
        camp.refresh_from_db()
        self.assertEqual((camp.partidos.count(), camp.fechas.count(), camp.modalidad, camp.estado), (0, 0, 'IDA', 'BORRADOR'))

    def test_bloqueado_si_un_partido_comenzo(self):
        camp = crear_campeonato(4, 'LimBloq')
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        p.estado = 'INICIADO'
        p.save()
        with self.assertRaises(fixture.FixtureError):
            fixture.limpiar_sorteo(camp)
        self.assertEqual(camp.partidos.count(), 6)

    def test_api_solo_administrador(self):
        User = get_user_model()
        op = User.objects.create_user('opl', password='clave-larga-1')
        op.groups.add(Group.objects.create(name='Operador'))
        admin = User.objects.create_superuser('rootl', password='clave-larga-1')
        camp = crear_campeonato(4, 'LimApi', publicado=True)
        fixture.generar_fixture(camp)
        url = f'/api/v1/campeonatos/{camp.id}/limpiar-sorteo/'
        self.client.force_authenticate(op)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_authenticate(admin)
        r = self.client.post(url)
        self.assertEqual((r.status_code, r.data['partidos']), (200, 6))
        self.assertEqual(camp.partidos.count(), 0)


class ProgramarRondaTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('rootp', password='clave-larga-1')
        self.op = User.objects.create_user('opp', password='clave-larga-1')
        self.op.groups.add(Group.objects.create(name='Operador'))
        self.camp = crear_campeonato(4, 'Prog', publicado=True)
        [r1] = fixture.generar_fixture(self.camp, modalidad='RELAMPAGO')
        for p in r1.partidos.all():
            jugar(p, 1, 0)
        self.final = avanzar_eliminatoria(self.camp)
        self.url = f'/api/v1/fechas/{self.final.id}/programar/'
        self.partido = self.final.partidos.get()

    def test_admin_programa_la_final(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(self.url, {'partidos': [{'id': self.partido.id, 'fecha_hora': '2026-11-05T20:30:00-05:00'}]},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.partido.refresh_from_db()
        self.final.refresh_from_db()
        self.assertEqual(self.partido.fecha_hora.isoformat(), '2026-11-06T01:30:00+00:00')
        self.assertEqual(str(self.final.fecha_programada), '2026-11-05')
        self.assertEqual(r.data['partidos'][0]['id'], self.partido.id)

    def test_validaciones(self):
        self.client.force_authenticate(self.admin)
        casos = [
            {'partidos': []},
            {'partidos': [{'id': self.partido.id, 'fecha_hora': 'mañana'}]},
            {'partidos': [{'id': self.partido.id}]},
            {'partidos': [{'id': 99999, 'fecha_hora': '2026-11-05T20:30:00-05:00'}]},
        ]
        for cuerpo in casos:
            self.assertEqual(self.client.post(self.url, cuerpo, format='json').status_code, 400, cuerpo)
        self.partido.estado = 'INICIADO'
        self.partido.save()
        r = self.client.post(self.url, {'partidos': [{'id': self.partido.id, 'fecha_hora': '2026-11-05T20:30:00-05:00'}]},
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_el_operador_no_programa_rondas(self):
        self.client.force_authenticate(self.op)
        r = self.client.post(self.url, {'partidos': [{'id': self.partido.id, 'fecha_hora': '2026-11-05T20:30:00-05:00'}]},
                             format='json')
        self.assertEqual(r.status_code, 403)


class RolesYPropietariosTests(APITestCase):
    """Organizadores: cada uno ve y gestiona sólo lo suyo; lo anterior sigue igual (del sistema / Administrador)."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        User = get_user_model()
        for nombre in ('Administrador', 'Organizador', 'Operador'):
            Group.objects.get_or_create(name=nombre)
        self.admin = User.objects.create_superuser('admin_r', password='Clave-larga-1')
        self.org1 = self.usuario('org1', 'Organizador')
        self.org2 = self.usuario('org2', 'Organizador')
        self.op_global = self.usuario('op_global', 'Operador')                 # como los de antes
        self.sistema = self.datos(None, 'Sis', publicado=True)                  # del sistema (sin dueño)
        self.c1 = self.datos(self.org1, 'O1', publicado=True)
        self.c2 = self.datos(self.org2, 'O2', publicado=False)

    def usuario(self, nombre, rol, **kw):
        user = get_user_model().objects.create_user(nombre, password='Clave-larga-1', **kw)
        user.groups.add(Group.objects.get(name=rol))
        return user

    def datos(self, propietario, prefijo, n=4, publicado=False):
        camp = Campeonato.objects.create(nombre=f'Camp {prefijo}', propietario=propietario, publicado=publicado)
        for i in range(n):
            e = Equipo.objects.create(nombre=f'{prefijo} {i}', propietario=propietario)
            Inscripcion.objects.create(campeonato=camp, equipo=e)
        return camp

    def como(self, user):
        self.client.force_authenticate(user)
        return self.client

    def ids(self, respuesta):
        return {x['id'] for x in respuesta.data['results']}

    # ---- aislamiento
    def test_cada_quien_ve_solo_lo_suyo(self):
        self.assertEqual(self.ids(self.como(self.org1).get('/api/v1/campeonatos/')), {self.c1.id})
        self.assertEqual(self.ids(self.como(self.org2).get('/api/v1/campeonatos/')), {self.c2.id})
        self.assertEqual(self.ids(self.como(self.admin).get('/api/v1/campeonatos/')),
                         {self.sistema.id, self.c1.id, self.c2.id})
        self.assertEqual(self.ids(self.como(self.op_global).get('/api/v1/campeonatos/')),
                         {self.sistema.id, self.c1.id, self.c2.id})  # el operador de antes no cambia
        self.client.force_authenticate(None)
        self.assertEqual(self.ids(self.client.get('/api/v1/campeonatos/')), {self.sistema.id, self.c1.id})  # público

    def test_lo_ajeno_responde_404_en_todo(self):
        c = self.como(self.org1)
        ajeno = self.c2
        e_ajeno = ajeno.inscripciones.first().equipo
        for url in (f'/api/v1/campeonatos/{ajeno.id}/', f'/api/v1/campeonatos/{ajeno.id}/calendario/',
                    f'/api/v1/campeonatos/{ajeno.id}/tabla/', f'/api/v1/campeonatos/{ajeno.id}/calendario-pdf/',
                    f'/api/v1/campeonatos/{ajeno.id}/fixture-info/', f'/api/v1/equipos/{e_ajeno.id}/'):
            self.assertEqual(c.get(url).status_code, 404, url)
        for url in (f'/api/v1/campeonatos/{ajeno.id}/generar-fixture/', f'/api/v1/campeonatos/{ajeno.id}/limpiar-sorteo/',
                    f'/api/v1/campeonatos/{ajeno.id}/equipos/', f'/api/v1/campeonatos/{ajeno.id}/aplicar-feriados/'):
            self.assertEqual(c.post(url, {}, format='json').status_code, 404, url)
        self.assertEqual(c.patch(f'/api/v1/campeonatos/{ajeno.id}/', {'nombre': 'x'}, format='json').status_code, 404)
        self.assertEqual(c.delete(f'/api/v1/campeonatos/{ajeno.id}/').status_code, 404)
        self.assertEqual(c.delete(f'/api/v1/equipos/{e_ajeno.id}/').status_code, 404)
        self.assertTrue(Campeonato.objects.filter(pk=ajeno.pk).exists())

    def test_equipos_jugadores_partidos_eventos_y_notificaciones_son_propios(self):
        for camp in (self.c1, self.c2):
            fixture.generar_fixture(camp)
            p = camp.partidos.first()
            j = Jugador.objects.create(equipo=p.local, nombres='X', apellidos=camp.nombre, numero=1)
            EventoPartido.objects.create(partido=p, tipo='GOL', equipo=p.local, jugador=j, minuto=3)
            p.estado = 'INICIADO'
            p.save()
        c = self.como(self.org1)
        # notificaciones: el gol registrado y el cambio de estado del partido
        for recurso, esperado in (('equipos', 4), ('jugadores', 1), ('partidos', 6), ('eventos', 1),
                                  ('notificaciones', 2), ('fechas', 3)):
            r = c.get(f'/api/v1/{recurso}/')
            self.assertEqual(len(r.data['results']), esperado, recurso)
        self.assertEqual(len(self.como(self.admin).get('/api/v1/equipos/').data['results']), 12)

    def test_publico_ve_equipos_solo_de_campeonatos_publicados(self):
        self.client.force_authenticate(None)
        nombres = {e['nombre'] for e in self.client.get('/api/v1/equipos/').data['results']}
        self.assertEqual(len(nombres), 8)                                  # sistema (publicado) + org1 (publicado)
        self.assertFalse(any(n.startswith('O2') for n in nombres))          # el de org2 (no publicado) no se ve

    # ---- crear y propiedad
    def test_el_organizador_crea_campeonato_y_equipos_a_su_nombre(self):
        c = self.como(self.org1)
        r = c.post('/api/v1/campeonatos/', {'nombre': 'Mi copa', 'propietario': self.org2.id}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['propietario'], self.org1.id)               # no puede elegir otro dueño
        e = c.post('/api/v1/equipos/', {'nombre': 'Águilas'}, format='json')
        self.assertEqual((e.status_code, e.data['propietario']), (201, self.org1.id))
        self.assertEqual(c.post('/api/v1/equipos/', {'nombre': 'águilas'}, format='json').status_code, 400)  # repetido
        otro = self.como(self.org2).post('/api/v1/equipos/', {'nombre': 'Águilas'}, format='json')
        self.assertEqual(otro.status_code, 201)                               # otro organizador sí puede
        self.assertEqual(self.como(self.admin).post('/api/v1/equipos/', {'nombre': 'Águilas'}, format='json').status_code, 201)

    def test_no_puede_cambiar_el_propietario(self):
        r = self.como(self.org1).patch(f'/api/v1/campeonatos/{self.c1.id}/', {'propietario': self.org2.id}, format='json')
        self.assertEqual(r.status_code, 200)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.propietario, self.org1)

    def test_el_administrador_crea_a_nombre_de_un_organizador(self):
        c = self.como(self.admin)
        r = c.post('/api/v1/campeonatos/', {'nombre': 'Hecha por admin', 'propietario': self.org1.id}, format='json')
        self.assertEqual((r.status_code, r.data['propietario']), (201, self.org1.id))
        self.assertEqual(r.data['propietario_nombre'], 'org1')
        sin = c.post('/api/v1/campeonatos/', {'nombre': 'Del sistema'}, format='json')
        self.assertIsNone(sin.data['propietario'])

    # ---- inscripción
    def test_solo_se_inscriben_equipos_del_mismo_dueno(self):
        camp = Campeonato.objects.create(nombre='Nueva', propietario=self.org1)
        propios = [Equipo.objects.create(nombre=f'P{i}', propietario=self.org1).id for i in range(2)]
        ajeno = self.c2.inscripciones.first().equipo_id
        del_sistema = self.sistema.inscripciones.first().equipo_id
        c = self.como(self.org1)
        url = f'/api/v1/campeonatos/{camp.id}/equipos/'
        self.assertEqual(c.post(url, {'equipos': propios + [ajeno]}, format='json').status_code, 400)
        self.assertEqual(c.post(url, {'equipos': propios + [del_sistema]}, format='json').status_code, 400)
        self.assertEqual(c.post(url, {'equipos': propios}, format='json').status_code, 200)
        self.assertEqual(camp.inscripciones.count(), 2)
        # ni siquiera el administrador mezcla dueños
        self.assertEqual(self.como(self.admin).post(url, {'equipos': [ajeno]}, format='json').status_code, 400)
        # el campeonato del sistema sólo admite equipos del sistema
        self.assertEqual(self.como(self.admin).post(f'/api/v1/campeonatos/{self.sistema.id}/equipos/',
                                                    {'equipos': [propios[0]]}, format='json').status_code, 400)

    def test_flujo_completo_de_un_organizador(self):
        c = self.como(self.org1)
        camp = c.post('/api/v1/campeonatos/', {'nombre': 'Copa propia', 'publicado': True}, format='json').data
        ids = [c.post('/api/v1/equipos/', {'nombre': f'Mío {i}'}, format='json').data['id'] for i in range(4)]
        self.assertEqual(c.post(f'/api/v1/campeonatos/{camp["id"]}/equipos/', {'equipos': ids}, format='json').status_code, 200)
        self.assertEqual(c.post(f'/api/v1/campeonatos/{camp["id"]}/generar-fixture/',
                                {'modalidad': 'IDA', 'fecha_inicio': '2026-10-13', 'dias_semana': [2]}, format='json').status_code, 201)
        partido = c.get(f'/api/v1/partidos/?campeonato={camp["id"]}').data['results'][0]
        self.assertEqual(c.post(f'/api/v1/partidos/{partido["id"]}/cambiar-estado/', {'estado': 'INICIADO'}, format='json').status_code, 200)
        self.assertEqual(c.post('/api/v1/eventos/', {'partido': partido['id'], 'tipo': 'GOL', 'equipo': partido['local'], 'minuto': 5},
                                format='json').status_code, 201)
        self.assertEqual(c.get(f'/api/v1/campeonatos/{camp["id"]}/tabla/').status_code, 200)
        self.assertEqual(c.get(f'/api/v1/campeonatos/{camp["id"]}/calendario-pdf/').status_code, 200)
        self.assertEqual(c.get(f'/api/v1/campeonatos/{camp["id"]}/horarios-fijos/').status_code, 405)
        self.assertEqual(self.como(self.org2).get(f'/api/v1/campeonatos/{camp["id"]}/').status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f'/api/v1/campeonatos/{camp["id"]}/').status_code, 200)  # publicado: lo ve el público

    def test_no_se_puede_usar_datos_ajenos_al_crear(self):
        c = self.como(self.org1)
        ajeno_e = self.c2.inscripciones.first().equipo
        self.assertEqual(c.post('/api/v1/jugadores/', {'equipo': ajeno_e.id, 'nombres': 'A', 'apellidos': 'B'}, format='json').status_code, 400)
        propio_e = self.c1.inscripciones.first().equipo
        self.assertEqual(c.post('/api/v1/jugadores/', {'equipo': propio_e.id, 'nombres': 'A', 'apellidos': 'B'}, format='json').status_code, 201)
        fixture.generar_fixture(self.c2)
        p_ajeno = self.c2.partidos.first()
        r = c.post('/api/v1/eventos/', {'partido': p_ajeno.id, 'tipo': 'GOL', 'equipo': p_ajeno.local_id, 'minuto': 1}, format='json')
        self.assertIn(r.status_code, (400, 404))
        self.assertEqual(c.post('/api/v1/partidos/', {'campeonato': self.c2.id, 'fecha': 1, 'local': 1, 'visitante': 2}, format='json').status_code, 400)

    # ---- usuarios y operadores
    def test_el_organizador_crea_operadores_limitados_a_sus_campeonatos(self):
        c = self.como(self.org1)
        r = c.post('/api/v1/usuarios/', {'username': 'ope1', 'password': 'Clave-larga-1', 'rol': 'Operador',
                                         'campeonatos': [self.c1.id]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['roles'], r.data['restringido'], r.data['creado_por_nombre']), (['Operador'], True, 'org1'))
        self.assertEqual(c.post('/api/v1/usuarios/', {'username': 'x', 'password': 'Clave-larga-1', 'rol': 'Administrador'},
                                format='json').status_code, 400)           # no puede crear otros roles
        self.assertEqual(c.post('/api/v1/usuarios/', {'username': 'y', 'password': 'Clave-larga-1', 'rol': 'Operador',
                                                      'campeonatos': [self.c2.id]}, format='json').status_code, 400)  # ni asignar ajenos
        self.assertEqual([u['username'] for u in c.get('/api/v1/usuarios/').data['results']], ['ope1'])
        self.assertEqual(self.como(self.org2).get('/api/v1/usuarios/').data['results'], [])
        self.assertEqual(self.como(self.org2).patch(f'/api/v1/usuarios/{r.data["id"]}/', {'is_active': False}, format='json').status_code, 404)

    def test_el_operador_restringido_solo_opera_sus_campeonatos(self):
        fixture.generar_fixture(self.c1)
        fixture.generar_fixture(self.c2)
        ope = self.como(self.org1).post('/api/v1/usuarios/', {'username': 'ope2', 'password': 'Clave-larga-1', 'rol': 'Operador',
                                                              'campeonatos': [self.c1.id]}, format='json')
        operador = get_user_model().objects.get(pk=ope.data['id'])
        c = self.como(operador)
        self.assertEqual(self.ids(c.get('/api/v1/campeonatos/')), {self.c1.id})
        self.assertEqual(len(c.get('/api/v1/partidos/').data['results']), 6)
        propio = self.c1.partidos.first()
        self.assertEqual(c.post(f'/api/v1/partidos/{propio.id}/cambiar-estado/', {'estado': 'INICIADO'}, format='json').status_code, 200)
        ajeno = self.c2.partidos.first()
        self.assertEqual(c.post(f'/api/v1/partidos/{ajeno.id}/cambiar-estado/', {'estado': 'INICIADO'}, format='json').status_code, 404)
        self.assertEqual(c.post('/api/v1/equipos/', {'nombre': 'No'}, format='json').status_code, 403)   # no administra equipos
        self.assertEqual(c.post('/api/v1/campeonatos/', {'nombre': 'No'}, format='json').status_code, 403)
        self.assertEqual(c.get('/api/v1/usuarios/').status_code, 403)

    def test_el_operador_de_antes_sigue_operando_todo(self):
        fixture.generar_fixture(self.sistema)
        fixture.generar_fixture(self.c2)
        c = self.como(self.op_global)
        self.assertEqual(len(c.get('/api/v1/partidos/').data['results']), 12)
        p = self.c2.partidos.first()
        self.assertEqual(c.post(f'/api/v1/partidos/{p.id}/cambiar-estado/', {'estado': 'INICIADO'}, format='json').status_code, 200)

    def test_el_administrador_gestiona_todos_los_usuarios(self):
        c = self.como(self.admin)
        nombres = {u['username'] for u in c.get('/api/v1/usuarios/').data['results']}
        self.assertTrue({'org1', 'org2', 'op_global', 'admin_r'} <= nombres)
        r = c.post('/api/v1/usuarios/', {'username': 'nuevo_org', 'password': 'Clave-larga-1', 'rol': 'Organizador'}, format='json')
        self.assertEqual((r.status_code, r.data['roles']), (201, ['Organizador']))
        # asignar campeonatos a un operador de antes lo vuelve restringido; quitarlos lo devuelve a global
        pid = self.op_global.id
        self.assertTrue(c.patch(f'/api/v1/usuarios/{pid}/', {'campeonatos': [self.c1.id]}, format='json').data['restringido'])
        self.assertFalse(c.patch(f'/api/v1/usuarios/{pid}/', {'campeonatos': []}, format='json').data['restringido'])

    def test_no_se_borra_un_usuario_con_campeonatos(self):
        c = self.como(self.admin)
        r = c.delete(f'/api/v1/usuarios/{self.org1.id}/')
        self.assertEqual((r.status_code, r.data['puede_desactivar']), (409, True))
        self.assertTrue(get_user_model().objects.filter(pk=self.org1.pk).exists())
        sin = self.usuario('sin_nada', 'Organizador')
        self.assertEqual(c.delete(f'/api/v1/usuarios/{sin.id}/').status_code, 204)

    # ---- auto-registro con aprobación
    def test_auto_registro_queda_pendiente_hasta_que_el_admin_lo_activa(self):
        self.client.force_authenticate(None)
        datos = {'username': 'nuevo1', 'password': 'Clave-larga-77', 'email': 'n@x.com', 'first_name': 'Nuevo'}
        r = self.client.post('/api/v1/auth/registro', datos, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        user = get_user_model().objects.get(username='nuevo1')
        self.assertFalse(user.is_active)
        self.assertTrue(user.groups.filter(name='Organizador').exists())
        login = self.client.post('/api/v1/auth/login', {'username': 'nuevo1', 'password': 'Clave-larga-77'}, format='json')
        self.assertEqual(login.status_code, 401)
        self.assertIn('administrador', str(login.data.get('detail', '')))   # mensaje claro, no «credenciales inválidas»
        c = self.como(self.admin)
        pend = c.get('/api/v1/usuarios/?pendientes=1').data['results']
        self.assertEqual([(u['username'], u['pendiente']) for u in pend], [('nuevo1', True)])
        self.assertTrue(c.patch(f'/api/v1/usuarios/{user.id}/', {'is_active': True}, format='json').data['is_active'])
        self.client.force_authenticate(None)
        ok = self.client.post('/api/v1/auth/login', {'username': 'nuevo1', 'password': 'Clave-larga-77'}, format='json')
        self.assertEqual((ok.status_code, ok.data['usuario']['roles']), (200, ['Organizador']))

    def test_registro_valida_los_datos(self):
        self.client.force_authenticate(None)
        base = {'username': 'val1', 'password': 'Clave-larga-77', 'email': 'v@x.com', 'first_name': 'V'}
        for cambio in ({'password': '123'}, {'password': 'password'}, {'email': 'no-es-correo'}, {'username': 'org1'},
                       {'username': 'ab'}, {'first_name': ''}):
            self.assertEqual(self.client.post('/api/v1/auth/registro', {**base, **cambio}, format='json').status_code, 400, cambio)
        self.assertFalse(get_user_model().objects.filter(username='val1').exists())

    def test_el_registro_tiene_limite_de_intentos(self):
        from torneos.views import RegistroThrottle
        self.client.force_authenticate(None)
        antes = RegistroThrottle.THROTTLE_RATES
        RegistroThrottle.THROTTLE_RATES = {'registro': '2/hour'}
        try:
            codigos = [self.client.post('/api/v1/auth/registro', {'username': f'lim{i}', 'password': 'Clave-larga-77',
                                                                    'email': f'l{i}@x.com', 'first_name': 'L'}, format='json').status_code
                       for i in range(4)]
        finally:
            RegistroThrottle.THROTTLE_RATES = antes
        self.assertEqual(codigos[:2], [201, 201])
        self.assertEqual(codigos[2], 429)

    # ---- feriados
    def test_feriados_del_organizador_y_generales_de_solo_lectura(self):
        from datetime import date
        from torneos.models import Feriado
        general = Feriado.objects.create(fecha=date(2026, 5, 1), nombre='Trabajo', se_repite=True)
        ajeno = Feriado.objects.create(fecha=date(2026, 6, 1), nombre='De org2', campeonato=self.c2)
        c = self.como(self.org1)
        self.assertEqual(c.post('/api/v1/feriados/', {'fecha': '2026-07-01', 'nombre': 'Mío', 'campeonato': self.c1.id}, format='json').status_code, 201)
        self.assertEqual(c.post('/api/v1/feriados/', {'fecha': '2026-07-02', 'nombre': 'General'}, format='json').status_code, 400)
        self.assertEqual(c.post('/api/v1/feriados/', {'fecha': '2026-07-03', 'nombre': 'Ajeno', 'campeonato': self.c2.id}, format='json').status_code, 400)
        nombres = {f['nombre'] for f in c.get('/api/v1/feriados/').data['results']}
        self.assertEqual(nombres, {'Trabajo', 'Mío'})                        # los generales + los propios; no los ajenos
        self.assertEqual(c.patch(f'/api/v1/feriados/{general.id}/', {'nombre': 'x'}, format='json').status_code, 403)
        self.assertEqual(c.delete(f'/api/v1/feriados/{general.id}/').status_code, 403)
        self.assertEqual(c.delete(f'/api/v1/feriados/{ajeno.id}/').status_code, 404)

    def test_login_y_me_informan_el_rol(self):
        self.client.force_authenticate(None)
        r = self.client.post('/api/v1/auth/login', {'username': 'org1', 'password': 'Clave-larga-1'}, format='json')
        self.assertEqual(r.data['usuario']['roles'], ['Organizador'])
        self.assertEqual(self.como(self.org1).get('/api/v1/auth/me').data['roles'], ['Organizador'])


CORREO_LOCAL = 'django.core.mail.backends.locmem.EmailBackend'


def _png(ancho=40, alto=40, ruido=False):
    from io import BytesIO
    import os as _os
    from PIL import Image
    buf = BytesIO()
    if ruido:  # contenido aleatorio: no se comprime, sirve para probar el límite de peso
        Image.frombytes('RGB', (ancho, alto), _os.urandom(ancho * alto * 3)).save(buf, 'PNG')
    else:
        Image.new('RGB', (ancho, alto), (30, 120, 60)).save(buf, 'PNG')
    return buf.getvalue()


def _mp4(peso=2048):
    return b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * peso


class CuentasAficionadoTests(APITestCase):
    """Registro con correo (con verificación), Google, recuperación de contraseña y perfil."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        for nombre in ('Administrador', 'Organizador', 'Operador', 'Aficionado'):
            Group.objects.get_or_create(name=nombre)
        self.cfg = ConfiguracionSistema.obtener()
        self.datos = {'email': 'Ana@Example.com', 'password': 'Clave-segura-2026', 'first_name': 'Ana', 'last_name': 'Pérez'}

    def configurar_correo(self):
        self.cfg.smtp_host, self.cfg.correo_remitente, self.cfg.url_publica = 'smtp.prueba.local', 'GoalScore <no-reply@prueba.local>', 'http://portal.test'
        self.cfg.save()

    def test_registro_sin_correo_configurado_queda_activo(self):
        r = self.client.post('/api/v1/auth/registro-aficionado', self.datos, format='json')
        self.assertEqual((r.status_code, r.data['requiere_verificacion']), (201, False))
        user = get_user_model().objects.get(username='ana@example.com')
        self.assertTrue(user.is_active)
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Aficionado'])
        login = self.client.post('/api/v1/auth/login', {'username': 'ana@example.com', 'password': 'Clave-segura-2026'}, format='json')
        self.assertEqual((login.status_code, login.data['usuario']['roles']), (200, ['Aficionado']))

    def test_registro_con_correo_configurado_exige_confirmar_el_enlace(self):
        from django.core import mail
        from django.test import override_settings
        self.configurar_correo()
        with override_settings(CORREO_BACKEND=CORREO_LOCAL):
            r = self.client.post('/api/v1/auth/registro-aficionado', self.datos, format='json')
            self.assertEqual((r.status_code, r.data['requiere_verificacion']), (201, True))
            user = get_user_model().objects.get(username='ana@example.com')
            self.assertFalse(user.is_active)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ['ana@example.com'])
            enlace = [p for p in mail.outbox[0].body.split() if p.startswith('http://portal.test/cuenta/verificar?token=')][0]
            login = self.client.post('/api/v1/auth/login', {'username': 'ana@example.com', 'password': 'Clave-segura-2026'}, format='json')
            self.assertEqual(login.status_code, 401)                                   # aún sin confirmar
            self.assertEqual(self.client.post('/api/v1/auth/verificar-correo', {'token': 'falso'}, format='json').status_code, 400)
            ok = self.client.post('/api/v1/auth/verificar-correo', {'token': enlace.split('token=')[1]}, format='json')
            self.assertEqual(ok.status_code, 200, ok.data)
            login = self.client.post('/api/v1/auth/login', {'username': 'ana@example.com', 'password': 'Clave-segura-2026'}, format='json')
            self.assertEqual(login.status_code, 200)

    def test_si_falla_el_envio_no_se_crea_la_cuenta(self):
        from unittest import mock
        from torneos.services.correo import CorreoError
        self.configurar_correo()
        with mock.patch('torneos.services.correo.enviar_verificacion', side_effect=CorreoError('x')):
            r = self.client.post('/api/v1/auth/registro-aficionado', self.datos, format='json')
        self.assertEqual(r.status_code, 503)
        self.assertFalse(get_user_model().objects.filter(username='ana@example.com').exists())

    def test_registro_valida_correo_y_clave(self):
        self.client.post('/api/v1/auth/registro-aficionado', self.datos, format='json')
        for cambio in ({'email': 'otro-no-es-correo'}, {'email': 'ANA@example.com'}, {'password': '123'}, {'first_name': ''}):
            datos = {**self.datos, 'email': 'nuevo@example.com', **cambio}
            self.assertEqual(self.client.post('/api/v1/auth/registro-aficionado', datos, format='json').status_code, 400, cambio)

    def test_google_crea_la_cuenta_y_no_toca_cuentas_del_personal(self):
        from unittest import mock
        self.client.post('/api/v1/auth/google', {'credential': 'x'}, format='json')
        self.assertEqual(self.client.post('/api/v1/auth/google', {'credential': 'x'}, format='json').status_code, 503)  # sin cliente configurado
        self.cfg.google_client_id = 'cliente.apps.googleusercontent.com'
        self.cfg.save()
        info = {'email': 'Luis@Gmail.com', 'email_verified': True, 'given_name': 'Luis', 'family_name': 'Mora'}
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value=info) as v:
            r = self.client.post('/api/v1/auth/google', {'credential': 'tok'}, format='json')
            self.assertEqual((r.status_code, r.data['usuario']['roles']), (200, ['Aficionado']))
            self.assertTrue(r.data['access'] and r.data['refresh'])
            self.assertEqual(v.call_args[0][2], 'cliente.apps.googleusercontent.com')
            self.assertEqual(self.client.post('/api/v1/auth/google', {'credential': 'tok'}, format='json').status_code, 200)  # entra de nuevo
        user = get_user_model().objects.get(username='luis@gmail.com')
        self.assertFalse(user.has_usable_password())
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value={**info, 'email_verified': False}):
            self.assertEqual(self.client.post('/api/v1/auth/google', {'credential': 'tok'}, format='json').status_code, 401)
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', side_effect=ValueError('mala')):
            self.assertEqual(self.client.post('/api/v1/auth/google', {'credential': 'tok'}, format='json').status_code, 401)
        admin = get_user_model().objects.create_superuser('jefe', email='jefe@x.com', password='Clave-larga-1')
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value={**info, 'email': admin.email}):
            self.assertEqual(self.client.post('/api/v1/auth/google', {'credential': 'tok'}, format='json').status_code, 403)

    def test_el_aficionado_no_gestiona_nada(self):
        user = get_user_model().objects.create_user('fan@x.com', email='fan@x.com', password='Clave-larga-1')
        user.groups.add(Group.objects.get(name='Aficionado'))
        camp = Campeonato.objects.create(nombre='Publico', publicado=True)
        Campeonato.objects.create(nombre='Oculto', publicado=False)
        self.client.force_authenticate(user)
        self.assertEqual([c['nombre'] for c in self.client.get('/api/v1/campeonatos/').data['results']], ['Publico'])
        for recurso in ('equipos', 'campeonatos', 'jugadores', 'feriados', 'medios'):
            self.assertEqual(self.client.post(f'/api/v1/{recurso}/', {'nombre': 'x'}, format='json').status_code, 403, recurso)
        self.assertEqual(self.client.get('/api/v1/usuarios/').status_code, 403)
        self.assertEqual(self.client.get('/api/v1/configuracion').status_code, 403)
        self.assertEqual(camp.partidos.count(), 0)

    def test_recuperar_contrasena_por_correo(self):
        from django.core import mail
        from django.test import override_settings
        self.configurar_correo()
        user = get_user_model().objects.create_user('ana@example.com', email='ana@example.com', password='Vieja-clave-1')
        with override_settings(CORREO_BACKEND=CORREO_LOCAL):
            self.client.post('/api/v1/auth/olvide-clave', {'email': 'nadie@example.com'}, format='json')
            self.assertEqual(len(mail.outbox), 0)                                  # no revela si el correo existe
            r = self.client.post('/api/v1/auth/olvide-clave', {'email': 'ana@example.com'}, format='json')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(mail.outbox), 1)
        enlace = [p for p in mail.outbox[0].body.split() if 'restablecer?' in p][0]
        uid, token = enlace.split('uid=')[1].split('&token=')
        url = '/api/v1/auth/restablecer-clave'
        self.assertEqual(self.client.post(url, {'uid': uid, 'token': 'mal', 'password': 'Nueva-clave-9'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'uid': uid, 'token': token, 'password': '123'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'uid': uid, 'token': token, 'password': 'Nueva-clave-9'}, format='json').status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('Nueva-clave-9'))
        self.assertEqual(self.client.post(url, {'uid': uid, 'token': token, 'password': 'Otra-clave-99'}, format='json').status_code, 400)  # un solo uso

    def test_perfil_y_cambio_de_clave(self):
        user = get_user_model().objects.create_user('p@x.com', email='p@x.com', password='Clave-larga-1', first_name='P')
        self.client.force_authenticate(user)
        r = self.client.patch('/api/v1/mi/perfil', {'first_name': 'Paula', 'email': 'otro@x.com'}, format='json')
        self.assertEqual((r.data['first_name'], r.data['email']), ('Paula', 'p@x.com'))     # el correo no se cambia aquí
        self.assertEqual(self.client.post('/api/v1/mi/cambiar-clave', {'actual': 'mal', 'nueva': 'Nueva-clave-9'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/mi/cambiar-clave', {'actual': 'Clave-larga-1', 'nueva': '123'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/mi/cambiar-clave', {'actual': 'Clave-larga-1', 'nueva': 'Nueva-clave-9'}, format='json').status_code, 200)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/v1/mi/perfil').status_code, 401)


class AvisosAAficionadosTests(APITestCase):
    """Intereses (todos o selección), dispositivos y envío de avisos al navegador."""

    def setUp(self):
        Group.objects.get_or_create(name='Aficionado')
        self.camp = crear_campeonato(4, 'Av', publicado=True)
        fixture.generar_fixture(self.camp, semilla=1)
        self.p = self.camp.partidos.first()
        self.otro = crear_campeonato(4, 'Otro', publicado=True)
        self.jugador = Jugador.objects.create(equipo=self.p.local, nombres='Juan', apellidos='Pérez', numero=9)
        self.fans = {}
        self.llamadas = []

    def fan(self, nombre, modo, tipos=None, campeonatos=(), equipos=()):
        user = get_user_model().objects.create_user(f'{nombre}@x.com', email=f'{nombre}@x.com', password='Clave-larga-1')
        user.groups.add(Group.objects.get(name='Aficionado'))
        prefs = PreferenciasNotificacion.objects.create(
            user=user, modo=modo, tipos=list(tipos if tipos is not None else ('ESTADO', 'GOL', 'TARJETA', 'CAMBIO', 'INCIDENCIA')))
        prefs.campeonatos.set(campeonatos)
        prefs.equipos.set(equipos)
        Dispositivo.objects.create(user=user, tipo='WEB', token=f'https://push.test/{nombre}', suscripcion={
            'endpoint': f'https://push.test/{nombre}', 'keys': {'p256dh': 'k', 'auth': 'a'}})
        self.fans[nombre] = user
        return user

    def enviar_con_mock(self, accion):
        from unittest import mock
        def falso(subscription_info, data, **kw):
            self.llamadas.append((subscription_info['endpoint'].rsplit('/', 1)[-1], json.loads(data)))
        with mock.patch('pywebpush.webpush', side_effect=falso):
            accion()
        return sorted(n for n, _ in self.llamadas)

    def test_cada_aficionado_recibe_solo_lo_que_eligio(self):
        M = PreferenciasNotificacion.Modo
        self.fan('todo', M.TODO)
        self.fan('camp', M.SELECCION, campeonatos=[self.camp])
        self.fan('equipo', M.SELECCION, equipos=[self.p.local])
        self.fan('rival', M.SELECCION, equipos=[self.p.visitante])
        self.fan('ajeno', M.SELECCION, campeonatos=[self.otro])
        self.fan('nada', M.NADA)
        self.fan('solo_goles', M.TODO, tipos=['GOL'])
        self.fan('sin_tipos', M.TODO, tipos=[])
        # cambio de estado: no le llega a quien sólo quiere goles ni a quien no quiere nada
        quienes = self.enviar_con_mock(lambda: (setattr(self.p, 'estado', 'INICIADO'), self.p.save()))
        self.assertEqual(quienes, ['camp', 'equipo', 'rival', 'todo'])
        cuerpo = self.llamadas[0][1]
        self.assertEqual((cuerpo['tipo'], cuerpo['url'], cuerpo['partido_id']), ('ESTADO', f'/partido/{self.p.id}', self.p.id))
        # gol: también le llega a quien sólo quiere goles
        self.llamadas.clear()
        quienes = self.enviar_con_mock(lambda: EventoPartido.objects.create(partido=self.p, tipo='GOL', equipo=self.p.local, jugador=self.jugador, minuto=12))
        self.assertEqual(quienes, ['camp', 'equipo', 'rival', 'solo_goles', 'todo'])
        self.assertIn('Juan Pérez', self.llamadas[0][1]['cuerpo'])
        self.assertIn(' 1 - 0 ', self.llamadas[0][1]['cuerpo'])                       # marcador ya actualizado
        # tarjeta amarilla y cambio
        self.llamadas.clear()
        self.enviar_con_mock(lambda: EventoPartido.objects.create(partido=self.p, tipo='AMARILLA', equipo=self.p.visitante, minuto=30))
        self.assertEqual(sorted(n for n, _ in self.llamadas), ['camp', 'equipo', 'rival', 'todo'])
        self.assertEqual(self.llamadas[0][1]['tipo'], 'TARJETA')
        self.assertIn('Tarjeta amarilla', self.llamadas[0][1]['titulo'])

    def test_no_avisa_de_campeonatos_sin_publicar_ni_de_partidos_cerrados(self):
        self.fan('todo', PreferenciasNotificacion.Modo.TODO)
        self.camp.publicado = False
        self.camp.save()
        self.assertEqual(self.enviar_con_mock(lambda: (setattr(self.p, 'estado', 'INICIADO'), self.p.save())), [])
        self.camp.publicado = True
        self.camp.save()
        self.p.estado = 'FINALIZADO'
        self.p.save()
        self.llamadas.clear()
        self.assertEqual(self.enviar_con_mock(lambda: EventoPartido.objects.create(partido=self.p, tipo='AMARILLA', equipo=self.p.local, minuto=5)), [])

    def test_correccion_manual_del_marcador_avisa(self):
        self.fan('todo', PreferenciasNotificacion.Modo.TODO)
        self.p.estado = 'INICIADO'
        self.p.save()
        self.llamadas.clear()
        def corregir():
            self.p.goles_local = 2
            self.p.save()
        self.assertEqual(self.enviar_con_mock(corregir), ['todo'])
        self.assertEqual(self.llamadas[0][1]['tipo'], 'GOL')
        self.assertIn('Marcador actualizado', self.llamadas[0][1]['titulo'])

    def test_envio_web_push_real_cifrado_y_firmado_con_vapid(self):
        """Sin mocks: un «navegador» simulado (claves EC propias) recibe el aviso por HTTP, lo descifra y se valida la firma VAPID."""
        import base64
        import http.server
        import threading
        import http_ece
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from torneos.services import push

        def b64(datos):
            return base64.urlsafe_b64encode(datos).rstrip(b'=').decode()

        def sin_b64(texto):
            return base64.urlsafe_b64decode(texto + '=' * (-len(texto) % 4))

        clave_navegador = ec.generate_private_key(ec.SECP256R1())
        secreto_auth = b'0123456789abcdef'
        recibido = {}

        class ServicioPush(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                recibido['cabeceras'] = {k.lower(): v for k, v in self.headers.items()}   # sin distinguir mayúsculas
                recibido['cuerpo'] = self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *a):
                pass

        servidor = http.server.HTTPServer(('127.0.0.1', 0), ServicioPush)
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)

        user = self.fan('real', PreferenciasNotificacion.Modo.TODO)
        endpoint = f'http://127.0.0.1:{servidor.server_address[1]}/push/abc'
        Dispositivo.objects.filter(user=user).update(token=endpoint, suscripcion={'endpoint': endpoint, 'keys': {
            'p256dh': b64(clave_navegador.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
            'auth': b64(secreto_auth)}})
        config = ConfiguracionSistema.obtener()
        config.vapid_contacto = 'mailto:soporte@prueba.local'
        config.save()
        push.asegurar_vapid(config)

        self.p.estado = 'INICIADO'
        self.p.save()                     # dispara el aviso real

        # 1) el mensaje llegó cifrado y se descifra con las claves de la suscripción
        self.assertIn('cuerpo', recibido)
        self.assertEqual(recibido['cabeceras']['content-encoding'], 'aes128gcm')
        self.assertNotIn(b'Comenz', recibido['cuerpo'])                                # no viaja en claro
        texto = http_ece.decrypt(recibido['cuerpo'], private_key=clave_navegador, auth_secret=secreto_auth, version='aes128gcm')
        carga = json.loads(texto.decode())
        self.assertEqual((carga['tipo'], carga['url'], carga['partido_id']), ('ESTADO', f'/partido/{self.p.id}', self.p.id))
        self.assertIn('Comenzó', carga['cuerpo'])
        # 2) la firma VAPID es válida con la clave pública que se entrega al navegador
        autorizacion = recibido['cabeceras']['authorization']
        self.assertTrue(autorizacion.startswith('vapid '))
        partes = dict(p.strip().split('=', 1) for p in autorizacion[len('vapid '):].split(','))
        self.assertEqual(partes['k'], ConfiguracionSistema.obtener().vapid_publica)
        encabezado, cuerpo_jwt, firma = partes['t'].split('.')
        reclamos = json.loads(sin_b64(cuerpo_jwt))
        self.assertEqual((reclamos['aud'], reclamos['sub']), (f'http://127.0.0.1:{servidor.server_address[1]}', 'mailto:soporte@prueba.local'))
        raw = sin_b64(firma)
        publica = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sin_b64(partes['k']))
        publica.verify(utils.encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
                       f'{encabezado}.{cuerpo_jwt}'.encode(), ec.ECDSA(hashes.SHA256()))   # lanza si no es válida

    def test_suscripcion_vencida_se_elimina(self):
        from unittest import mock
        from pywebpush import WebPushException
        self.fan('viejo', PreferenciasNotificacion.Modo.TODO)
        error = WebPushException('gone', response=mock.Mock(status_code=410))
        with mock.patch('pywebpush.webpush', side_effect=error):
            self.p.estado = 'INICIADO'
            self.p.save()
        self.assertFalse(Dispositivo.objects.filter(token='https://push.test/viejo').exists())

    def test_el_registro_queda_con_destinatarios(self):
        self.fan('a', PreferenciasNotificacion.Modo.TODO)
        self.fan('b', PreferenciasNotificacion.Modo.TODO)
        self.enviar_con_mock(lambda: (setattr(self.p, 'estado', 'INICIADO'), self.p.save()))
        noti = Notificacion.objects.filter(partido=self.p, tipo='ESTADO').latest('id')
        self.assertEqual((noti.destinatarios, noti.enviada), (2, True))

    def test_api_de_intereses(self):
        user = self.fan('yo', PreferenciasNotificacion.Modo.NADA)
        self.client.force_authenticate(user)
        sel = {'modo': 'SELECCION', 'tipos': ['GOL', 'TARJETA'], 'campeonatos': [self.camp.id], 'equipos': [self.p.local_id]}
        r = self.client.put('/api/v1/mi/preferencias', sel, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['modo'], r.data['tipos'], r.data['campeonatos_detalle'][0]['nombre']), ('SELECCION', ['GOL', 'TARJETA'], self.camp.nombre))
        self.assertEqual(self.client.get('/api/v1/mi/preferencias').data['equipos'], [self.p.local_id])
        oculto = Campeonato.objects.create(nombre='Oculto', publicado=False)
        for malo in ({'modo': 'X'}, {'tipos': ['BAILE']}, {'campeonatos': [oculto.id]}, {'equipos': [Equipo.objects.create(nombre='Sin publicar').id]}):
            self.assertEqual(self.client.put('/api/v1/mi/preferencias', malo, format='json').status_code, 400, malo)
        self.assertEqual(self.client.put('/api/v1/mi/preferencias', {'modo': 'TODO', 'campeonatos': [], 'equipos': []}, format='json').data['modo'], 'TODO')

    def test_api_de_dispositivos(self):
        user = self.fan('yo', PreferenciasNotificacion.Modo.TODO)
        self.client.force_authenticate(user)
        web = {'tipo': 'WEB', 'nombre': 'Chrome', 'suscripcion': {'endpoint': 'https://push.test/nuevo', 'keys': {'p256dh': 'k', 'auth': 'a'}}}
        r = self.client.post('/api/v1/mi/dispositivos', web, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.client.post('/api/v1/mi/dispositivos', {'tipo': 'WEB', 'suscripcion': {'endpoint': 'x'}}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/mi/dispositivos', {'tipo': 'FCM'}, format='json').status_code, 400)
        fcm = self.client.post('/api/v1/mi/dispositivos', {'tipo': 'FCM', 'token': 'token-fcm-123', 'nombre': 'Pixel'}, format='json')
        self.assertEqual(fcm.status_code, 201)
        self.assertEqual(len(self.client.get('/api/v1/mi/dispositivos').data), 3)       # el del setUp + 2
        # el mismo navegador pasa a la cuenta que lo registra (no se duplica)
        otra = get_user_model().objects.create_user('otra@x.com', password='Clave-larga-1')
        self.client.force_authenticate(otra)
        self.client.post('/api/v1/mi/dispositivos', web, format='json')
        self.assertEqual(Dispositivo.objects.filter(token='https://push.test/nuevo').count(), 1)
        self.assertEqual(Dispositivo.objects.get(token='https://push.test/nuevo').user, otra)
        self.assertEqual(self.client.delete(f'/api/v1/mi/dispositivos/{r.data["id"]}').status_code, 204)
        self.assertEqual(self.client.delete(f'/api/v1/mi/dispositivos/{fcm.data["id"]}').status_code, 404)  # no es suyo

    def test_aviso_de_prueba(self):
        from unittest import mock
        user = self.fan('yo', PreferenciasNotificacion.Modo.NADA)
        self.client.force_authenticate(user)
        with mock.patch('pywebpush.webpush') as w:
            r = self.client.post('/api/v1/mi/probar-notificacion')
        self.assertEqual((r.status_code, r.data['enviados']), (200, 1))
        self.assertEqual(w.call_count, 1)
        Dispositivo.objects.all().delete()
        self.assertEqual(self.client.post('/api/v1/mi/probar-notificacion').status_code, 400)

    def test_configuracion_publica_entrega_la_clave_para_el_navegador(self):
        r = self.client.get('/api/v1/publico/configuracion')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(len(r.data['vapid_publica']) > 60)
        self.assertEqual(r.data['limites']['max_imagenes_partido'], 10)
        self.assertEqual(self.client.get('/api/v1/publico/configuracion').data['vapid_publica'], r.data['vapid_publica'])  # estable
        self.assertEqual(r.data['tipos_notificacion'], ['ESTADO', 'GOL', 'TARJETA', 'CAMBIO', 'INCIDENCIA'])


class ConfiguracionYCorreoTests(APITestCase):
    def setUp(self):
        for nombre in ('Administrador', 'Organizador'):
            Group.objects.get_or_create(name=nombre)
        self.admin = get_user_model().objects.create_superuser('cfg_admin', password='Clave-larga-1')
        self.org = get_user_model().objects.create_user('cfg_org', password='Clave-larga-1')
        self.org.groups.add(Group.objects.get(name='Organizador'))
        self.client.force_authenticate(self.admin)
        self.smtp = {'smtp_host': 'smtp.prueba.local', 'smtp_puerto': 587, 'smtp_usuario': 'usuario@prueba.local',
                     'smtp_clave': 'S3creta-App!', 'smtp_seguridad': 'STARTTLS', 'correo_remitente': 'GoalScore <no-reply@prueba.local>'}

    def test_guarda_la_configuracion_sin_exponer_ni_guardar_en_claro_la_clave(self):
        r = self.client.put('/api/v1/configuracion', self.smtp, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn('smtp_clave', r.data)
        self.assertTrue(r.data['tiene_clave'] and r.data['correo_configurado'])
        guardado = ConfiguracionSistema.obtener().smtp_clave
        self.assertTrue(guardado.startswith('enc:') and 'S3creta' not in guardado)         # cifrada en la base
        from torneos.services.cifrado import descifrar
        self.assertEqual(descifrar(guardado), 'S3creta-App!')
        # clave vacía = conservar la actual
        self.client.put('/api/v1/configuracion', {'smtp_host': 'otro.local', 'smtp_clave': ''}, format='json')
        self.assertEqual(descifrar(ConfiguracionSistema.obtener().smtp_clave), 'S3creta-App!')
        self.assertEqual(self.client.get('/api/v1/configuracion').data['smtp_host'], 'otro.local')

    def test_valida_puerto_y_limites(self):
        for malo in ({'smtp_puerto': 0}, {'smtp_puerto': 70000}, {'smtp_seguridad': 'RARA'}, {'max_mb_imagen': 0},
                     {'max_mb_video': 99999}, {'max_imagenes_partido': 500}):
            self.assertEqual(self.client.put('/api/v1/configuracion', malo, format='json').status_code, 400, malo)

    def test_solo_el_administrador_configura(self):
        self.client.force_authenticate(self.org)
        for metodo, url in (('get', '/api/v1/configuracion'), ('put', '/api/v1/configuracion'),
                            ('post', '/api/v1/configuracion/probar-correo'), ('post', '/api/v1/configuracion/regenerar-vapid')):
            self.assertEqual(getattr(self.client, metodo)(url, {}, format='json').status_code, 403, url)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/v1/configuracion').status_code, 401)

    def test_correo_de_prueba(self):
        from django.core import mail
        from django.test import override_settings
        self.client.put('/api/v1/configuracion', self.smtp, format='json')
        with override_settings(CORREO_BACKEND=CORREO_LOCAL):
            r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'yo@prueba.local'}, format='json')
        self.assertEqual((r.status_code, r.data['ok']), (200, True))
        self.assertEqual((len(mail.outbox), mail.outbox[0].to, mail.outbox[0].from_email),
                         (1, ['yo@prueba.local'], 'GoalScore <no-reply@prueba.local>'))
        self.assertIn('Prueba de correo', mail.outbox[0].subject)
        self.assertEqual(self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'sin-arroba'}, format='json').status_code, 400)

    def test_correo_de_prueba_explica_los_errores(self):
        import smtplib
        import socket
        from unittest import mock
        r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'yo@prueba.local'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('no está configurado', r.data['detail'])                           # sin servidor
        self.client.put('/api/v1/configuracion', self.smtp, format='json')
        casos = [(smtplib.SMTPAuthenticationError(535, b'x'), 'contraseña de aplicación'), (socket.gaierror(), 'nombre del servidor'),
                 (socket.timeout(), 'tardó demasiado'), (ConnectionRefusedError(), 'puerto'), (smtplib.SMTPServerDisconnected(), 'conexión segura')]
        for error, texto in casos:
            with mock.patch('torneos.services.correo._conexion', side_effect=error):
                r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'yo@prueba.local'}, format='json')
            self.assertEqual((r.status_code, r.data['ok']), (400, False))
            self.assertIn(texto, r.data['detail'], texto)

    def _servidor_smtp(self, clave_correcta):
        """Mini servidor SMTP con autenticación (PLAIN/LOGIN) para probar el envío real, sin red externa."""
        import base64
        import socketserver
        import threading
        recibido = {'mensajes': [], 'credenciales': []}

        class Manejador(socketserver.StreamRequestHandler):
            def responder(self, texto):
                self.wfile.write((texto + '\r\n').encode())

            def leer(self):
                return self.rfile.readline().decode().rstrip('\r\n')

            def handle(self):
                self.responder('220 prueba ESMTP')
                remitente, destinos = '', []
                while True:
                    linea = self.leer()
                    if not linea:
                        return
                    orden = linea.upper()
                    if orden.startswith('EHLO') or orden.startswith('HELO'):
                        self.responder('250-prueba\r\n250-AUTH PLAIN LOGIN\r\n250 OK')
                    elif orden.startswith('AUTH PLAIN'):
                        _, usuario, clave = base64.b64decode(linea.split()[2]).decode().split('\x00')
                        self.autenticar(usuario, clave)
                    elif orden.startswith('AUTH LOGIN'):
                        self.responder('334 VXNlcm5hbWU6')
                        usuario = base64.b64decode(self.leer()).decode()
                        self.responder('334 UGFzc3dvcmQ6')
                        self.autenticar(usuario, base64.b64decode(self.leer()).decode())
                    elif orden.startswith('MAIL FROM'):
                        remitente = linea[10:].strip('<> ')
                        self.responder('250 OK')
                    elif orden.startswith('RCPT TO'):
                        destinos.append(linea[8:].strip('<> '))
                        self.responder('250 OK')
                    elif orden == 'DATA':
                        self.responder('354 fin con .')
                        lineas = []
                        while True:
                            actual = self.leer()
                            if actual == '.':
                                break
                            lineas.append(actual)
                        recibido['mensajes'].append({'from': remitente, 'to': list(destinos), 'datos': '\n'.join(lineas)})
                        self.responder('250 OK')
                    elif orden == 'QUIT':
                        self.responder('221 adiós')
                        return
                    else:
                        self.responder('250 OK')

            def autenticar(self, usuario, clave):
                recibido['credenciales'].append((usuario, clave))
                self.responder('235 autenticado' if clave == clave_correcta else '535 5.7.8 credenciales inválidas')

        servidor = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Manejador)
        servidor.daemon_threads = True
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)
        return servidor.server_address[1], recibido

    def test_envio_real_por_smtp_con_la_clave_guardada_cifrada(self):
        puerto, recibido = self._servidor_smtp('S3creta-App!')
        self.client.put('/api/v1/configuracion', {**self.smtp, 'smtp_host': '127.0.0.1', 'smtp_puerto': puerto,
                                                  'smtp_seguridad': 'NINGUNA'}, format='json')
        r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'destino@prueba.local'}, format='json')
        self.assertEqual((r.status_code, r.data['ok']), (200, True), r.data)
        self.assertEqual(recibido['credenciales'][-1], ('usuario@prueba.local', 'S3creta-App!'))   # la clave cifrada se descifró bien
        mensaje = recibido['mensajes'][0]
        self.assertEqual(mensaje['to'], ['destino@prueba.local'])
        self.assertIn('no-reply@prueba.local', mensaje['from'])
        self.assertIn('Subject:', mensaje['datos'])
        self.assertIn('text/html', mensaje['datos'])                                              # versión HTML incluida

    def test_clave_incorrecta_en_el_servidor_se_explica(self):
        puerto, recibido = self._servidor_smtp('otra-clave')
        self.client.put('/api/v1/configuracion', {**self.smtp, 'smtp_host': '127.0.0.1', 'smtp_puerto': puerto,
                                                  'smtp_seguridad': 'NINGUNA'}, format='json')
        r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'destino@prueba.local'}, format='json')
        self.assertEqual((r.status_code, r.data['ok']), (400, False))
        self.assertIn('contraseña de aplicación', r.data['detail'])
        self.assertEqual(recibido['mensajes'], [])

    def test_servidor_inexistente_o_puerto_cerrado(self):
        self.client.put('/api/v1/configuracion', {**self.smtp, 'smtp_host': '127.0.0.1', 'smtp_puerto': 1,
                                                  'smtp_seguridad': 'NINGUNA'}, format='json')
        r = self.client.post('/api/v1/configuracion/probar-correo', {'destino': 'destino@prueba.local'}, format='json')
        self.assertEqual((r.status_code, r.data['ok']), (400, False))
        self.assertIn('puerto', r.data['detail'])

    def test_regenerar_las_claves_del_navegador_invalida_las_suscripciones(self):
        pub1 = self.client.get('/api/v1/configuracion').data['vapid_publica']
        Dispositivo.objects.create(user=self.admin, tipo='WEB', token='https://push.test/x', suscripcion={'endpoint': 'https://push.test/x', 'keys': {}})
        Dispositivo.objects.create(user=self.admin, tipo='FCM', token='fcm-1')
        pub2 = self.client.post('/api/v1/configuracion/regenerar-vapid').data['vapid_publica']
        self.assertNotEqual(pub1, pub2)
        self.assertEqual(list(Dispositivo.objects.values_list('tipo', flat=True)), ['FCM'])   # sólo se invalidan las del navegador


class MediosDePartidoTests(APITestCase):
    """Fotos y videos: los suben el administrador y el organizador, con los límites de Configuración."""

    def setUp(self):
        import shutil
        import tempfile
        from django.test import override_settings
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        casilla = override_settings(MEDIA_ROOT=self.tmp)
        casilla.enable()
        self.addCleanup(casilla.disable)
        for nombre in ('Administrador', 'Organizador', 'Operador'):
            Group.objects.get_or_create(name=nombre)
        User = get_user_model()
        self.admin = User.objects.create_superuser('med_admin', password='Clave-larga-1')
        self.org = User.objects.create_user('med_org', password='Clave-larga-1')
        self.org.groups.add(Group.objects.get(name='Organizador'))
        self.op = User.objects.create_user('med_op', password='Clave-larga-1')
        self.op.groups.add(Group.objects.get(name='Operador'))
        self.camp = Campeonato.objects.create(nombre='Con medios', propietario=self.org, publicado=True)
        for i in range(2):
            Inscripcion.objects.create(campeonato=self.camp, equipo=Equipo.objects.create(nombre=f'M{i}', propietario=self.org))
        fixture.generar_fixture(self.camp)
        self.p = self.camp.partidos.first()
        self.url = '/api/v1/medios/'

    def subir(self, nombre, contenido, usuario=None, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_authenticate(usuario or self.admin)
        return self.client.post(self.url, {'partido': self.p.id, 'archivo': SimpleUploadedFile(nombre, contenido), **extra}, format='multipart')

    def test_sube_imagen_y_video_y_el_publico_los_ve(self):
        import os
        r = self.subir('gol.png', _png(), titulo='El gol')
        self.assertEqual((r.status_code, r.data['tipo'], r.data['titulo']), (201, 'IMAGEN', 'El gol'))
        self.assertTrue(r.data['archivo'].startswith('http') and r.data['archivo'].endswith('.png'))
        v = self.subir('resumen.mp4', _mp4())
        self.assertEqual((v.status_code, v.data['tipo']), (201, 'VIDEO'))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'partidos', str(self.p.id), 'gol.png')))
        self.client.force_authenticate(None)
        lista = self.client.get(f'/api/v1/medios/?partido={self.p.id}').data['results']
        self.assertEqual([m['tipo'] for m in lista], ['IMAGEN', 'VIDEO'])
        detalle = self.client.get(f'/api/v1/partidos/{self.p.id}/').data
        self.assertEqual((len(detalle['medios']), detalle['num_imagenes'], detalle['num_videos']), (2, 1, 1))
        self.camp.publicado = False
        self.camp.save()
        self.assertEqual(self.client.get(f'/api/v1/medios/?partido={self.p.id}').data['results'], [])   # sin publicar: no se ve

    def test_respeta_los_limites_que_define_el_administrador(self):
        cfg = ConfiguracionSistema.obtener()
        cfg.max_imagenes_partido, cfg.max_videos_partido, cfg.max_mb_imagen, cfg.max_mb_video = 2, 1, 1, 1
        cfg.save()
        self.assertEqual(self.subir('a.png', _png()).status_code, 201)
        self.assertEqual(self.subir('b.png', _png()).status_code, 201)
        r = self.subir('c.png', _png())
        self.assertEqual(r.status_code, 400)
        self.assertIn('máximo de imágenes', str(r.data['archivo']))
        self.assertEqual(self.subir('v1.mp4', _mp4()).status_code, 201)
        self.assertEqual(self.subir('v2.mp4', _mp4()).status_code, 400)
        # peso
        cfg.max_imagenes_partido = 10
        cfg.save()
        grande = _png(700, 700, ruido=True)
        self.assertGreater(len(grande), 1024 * 1024)
        r = self.subir('grande.png', grande)
        self.assertEqual(r.status_code, 400)
        self.assertIn('pesa', str(r.data['archivo']))
        self.assertEqual(self.p.medios.count(), 3)
        # subir el límite permite la imagen grande
        cfg.max_mb_imagen = 5
        cfg.save()
        self.assertEqual(self.subir('grande.png', grande).status_code, 201)

    def test_rechaza_archivos_que_no_son_lo_que_dicen(self):
        for nombre, contenido in (('virus.exe', b'MZ' + b'\x00' * 100), ('falsa.png', b'esto no es una imagen'),
                                  ('falso.mp4', b'texto cualquiera' * 10), ('sin_extension', _png()), ('doc.pdf', b'%PDF-1.4')):
            r = self.subir(nombre, contenido)
            self.assertEqual(r.status_code, 400, nombre)
        self.assertEqual(self.p.medios.count(), 0)

    def test_quien_puede_subir_y_borrar(self):
        self.assertEqual(self.subir('o.png', _png(), usuario=self.org).status_code, 201)          # su propio campeonato
        self.assertEqual(self.subir('op.png', _png(), usuario=self.op).status_code, 201)          # el operador sube desde el campo
        # un operador restringido a otro campeonato no puede subir a éste
        otro = Campeonato.objects.create(nombre='Otro campeonato', propietario=self.org, publicado=True)
        op_restringido = get_user_model().objects.create_user('med_op2', password='Clave-larga-1')
        op_restringido.groups.add(Group.objects.get(name='Operador'))
        perfil = PerfilUsuario.objects.get_or_create(user=op_restringido)[0]
        perfil.restringido = True
        perfil.save()
        perfil.campeonatos.add(otro)
        self.assertIn(self.subir('op2.png', _png(), usuario=op_restringido).status_code, (400, 404))
        # un aficionado no
        aficionado = get_user_model().objects.create_user('med_afi', password='Clave-larga-1')
        aficionado.groups.add(Group.objects.get_or_create(name='Aficionado')[0])
        self.assertEqual(self.subir('afi.png', _png(), usuario=aficionado).status_code, 403)
        ajeno_org = get_user_model().objects.create_user('otro_org', password='Clave-larga-1')
        ajeno_org.groups.add(Group.objects.get(name='Organizador'))
        self.assertIn(self.subir('x.png', _png(), usuario=ajeno_org).status_code, (400, 404))     # campeonato de otro
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url, {}, format='multipart').status_code, 401)
        medio = self.p.medios.filter(subido_por=self.org).get()
        self.client.force_authenticate(ajeno_org)
        self.assertEqual(self.client.delete(f'{self.url}{medio.id}/').status_code, 404)
        self.client.force_authenticate(self.org)
        self.assertEqual(self.client.patch(f'{self.url}{medio.id}/', {'titulo': 'Nuevo título'}, format='json').data['titulo'], 'Nuevo título')

    def test_borrar_elimina_tambien_el_archivo_del_disco(self):
        import os
        self.subir('borrar.png', _png())
        medio = self.p.medios.get()
        ruta = medio.archivo.path
        self.assertTrue(os.path.exists(ruta))
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f'{self.url}{medio.id}/').status_code, 204)
        self.assertFalse(os.path.exists(ruta))
        self.subir('otra.png', _png())
        ruta2 = self.p.medios.get().archivo.path
        self.p.delete()                                                                           # al borrar el partido también se limpian
        self.assertFalse(os.path.exists(ruta2))


class UsuariosYAficionadosTests(APITestCase):
    def test_los_aficionados_no_llenan_la_lista_del_administrador(self):
        for nombre in ('Administrador', 'Aficionado'):
            Group.objects.get_or_create(name=nombre)
        admin = get_user_model().objects.create_superuser('lst_admin', password='Clave-larga-1')
        fan = get_user_model().objects.create_user('fan@x.com', password='Clave-larga-1')
        fan.groups.add(Group.objects.get(name='Aficionado'))
        self.client.force_authenticate(admin)
        self.assertEqual([u['username'] for u in self.client.get('/api/v1/usuarios/').data['results']], ['lst_admin'])
        self.assertEqual([u['username'] for u in self.client.get('/api/v1/usuarios/?rol=Aficionado').data['results']], ['fan@x.com'])


class BorradoDeEquiposTests(APITestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('roote', password='clave-larga-1')
        self.client.force_authenticate(self.admin)

    def test_equipo_con_partidos_responde_409_claro_y_no_se_borra(self):
        camp = crear_campeonato(4, 'ConPartidos', publicado=True)
        fixture.generar_fixture(camp)
        equipo = camp.inscripciones.first().equipo
        r = self.client.delete(f'/api/v1/equipos/{equipo.id}/')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.data['puede_desactivar'])
        self.assertEqual(r.data['partidos'], 3)
        self.assertEqual(r.data['campeonatos'], ['Liga Test'])
        self.assertIn(equipo.nombre, r.data['detail'])
        self.assertTrue(Equipo.objects.filter(pk=equipo.pk).exists())

    def test_se_puede_desactivar_en_su_lugar(self):
        camp = crear_campeonato(4, 'Desact', publicado=True)
        fixture.generar_fixture(camp)
        equipo = camp.inscripciones.first().equipo
        r = self.client.patch(f'/api/v1/equipos/{equipo.id}/', {'activo': False}, format='json')
        self.assertEqual((r.status_code, r.data['activo']), (200, False))

    def test_tras_limpiar_el_sorteo_ya_se_puede_borrar(self):
        camp = crear_campeonato(4, 'Limpio', publicado=True)
        fixture.generar_fixture(camp)
        equipo = camp.inscripciones.first().equipo
        fixture.limpiar_sorteo(camp)
        self.assertEqual(self.client.delete(f'/api/v1/equipos/{equipo.id}/').status_code, 204)
        self.assertEqual(camp.inscripciones.count(), 3)  # sale también de la inscripción

    def test_equipo_sin_partidos_con_jugadores_se_borra(self):
        equipo = Equipo.objects.create(nombre='Libre')
        Jugador.objects.create(equipo=equipo, nombres='A', apellidos='B', numero=1)
        self.assertEqual(self.client.delete(f'/api/v1/equipos/{equipo.id}/').status_code, 204)
        self.assertFalse(Jugador.objects.exists())


class BorradoTests(APITestCase):
    def test_borrar_campeonato_con_partidos_y_eventos(self):
        admin = get_user_model().objects.create_superuser('root', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(4, 'Borra', publicado=True)
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        j = Jugador.objects.create(equipo=p.local, nombres='A', apellidos='B', numero=1)
        EventoPartido.objects.create(partido=p, tipo='GOL', equipo=p.local, jugador=j, minuto=3)
        EventoPartido.objects.create(partido=p, tipo='AMARILLA', equipo=p.local, jugador=j, minuto=4)
        p.estado = 'FINALIZADO'
        p.save()
        r = self.client.delete(f'/api/v1/campeonatos/{camp.id}/')
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Campeonato.objects.filter(id=camp.id).exists())
        self.assertFalse(EventoPartido.objects.exists())

    def test_borrar_partido_con_eventos(self):
        admin = get_user_model().objects.create_superuser('root2', password='clave-larga-1')
        self.client.force_authenticate(admin)
        camp = crear_campeonato(4, 'Borra2', publicado=True)
        fixture.generar_fixture(camp)
        p = camp.partidos.first()
        EventoPartido.objects.create(partido=p, tipo='GOL', equipo=p.local, minuto=3)
        self.assertEqual(self.client.delete(f'/api/v1/partidos/{p.id}/').status_code, 204)


class CalendarioPdfTests(APITestCase):
    def test_pdf_publico_con_todas_las_fechas(self):
        from datetime import date
        camp = crear_campeonato(5, 'Pdf', publicado=True)
        fixture.generar_fixture(camp, confirmar_descanso=True, fecha_inicio=date(2026, 10, 13), dias_semana=[2, 4])
        p = camp.partidos.first()
        p.estado, p.motivo_postergacion, p.fecha_hora = 'POSTERGADO', 'Lluvia', None
        p.save()
        r = self.client.get(f'/api/v1/campeonatos/{camp.id}/calendario-pdf/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertTrue(r.content.startswith(b'%PDF') and r.content.rstrip().endswith(b'%%EOF'))

    def test_pdf_de_campeonato_oculto_requiere_permiso(self):
        camp = crear_campeonato(4, 'Oculto')
        self.assertEqual(self.client.get(f'/api/v1/campeonatos/{camp.id}/calendario-pdf/').status_code, 404)

    def _png(self, ancho, alto):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new('RGBA', (ancho, alto), (64, 81, 137, 255)).save(buf, 'PNG')
        return buf.getvalue()

    def test_pdf_incluye_el_logo_del_campeonato(self):
        import shutil
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from torneos.services.pdf import calendario_pdf
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with override_settings(MEDIA_ROOT=tmp):
            sin = crear_campeonato(4, 'SinLogo', publicado=True)
            con = crear_campeonato(4, 'ConLogo', publicado=True)
            ancho = crear_campeonato(4, 'LogoAncho', publicado=True)
            con.logo.save('logo.png', SimpleUploadedFile('logo.png', self._png(200, 300)))
            ancho.logo.save('ancho.png', SimpleUploadedFile('ancho.png', self._png(900, 100)))
            for c in (sin, con, ancho):
                fixture.generar_fixture(c)
            pdf_sin, pdf_con, pdf_ancho = (calendario_pdf(c) for c in (sin, con, ancho))
        imagen = b'/Subtype /Image'
        base = pdf_sin.count(imagen)
        self.assertGreaterEqual(base, 1)  # todos llevan el logo de GoalScore en la cabecera
        self.assertGreater(pdf_con.count(imagen), base)  # el logo del campeonato es una imagen más
        self.assertGreater(pdf_ancho.count(imagen), base)  # logos muy anchos o altos también se ajustan

    def test_pdf_sigue_funcionando_si_el_archivo_del_logo_falta(self):
        from torneos.services.pdf import calendario_pdf
        camp = crear_campeonato(4, 'LogoRoto', publicado=True)
        camp.logo.name = 'campeonatos/no_existe.png'
        camp.save()
        fixture.generar_fixture(camp)
        sin_logo = crear_campeonato(4, 'LogoNinguno', publicado=True)
        fixture.generar_fixture(sin_logo)
        contenido = calendario_pdf(camp)
        self.assertTrue(contenido.startswith(b'%PDF'))
        # igual que un campeonato sin logo: sólo queda el de GoalScore
        self.assertEqual(contenido.count(b'/Subtype /Image'), calendario_pdf(sin_logo).count(b'/Subtype /Image'))

    def test_pdf_sin_calendario_generado(self):
        camp = crear_campeonato(4, 'Vacio', publicado=True)
        r = self.client.get(f'/api/v1/campeonatos/{camp.id}/calendario-pdf/')
        self.assertEqual(r.status_code, 200)


class ApiPermisosTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user('admin', password='clave-larga-1')
        self.admin.groups.add(Group.objects.create(name='Administrador'))
        self.op = User.objects.create_user('op', password='clave-larga-1')
        self.op.groups.add(Group.objects.create(name='Operador'))
        self.camp = crear_campeonato(4, publicado=True)
        self.oculto = Campeonato.objects.create(nombre='Oculto')

    def test_publico_solo_ve_publicados_y_no_escribe(self):
        r = self.client.get('/api/v1/campeonatos/')
        self.assertEqual([c['id'] for c in r.data['results']], [self.camp.id])
        self.assertEqual(self.client.get(f'/api/v1/campeonatos/{self.oculto.id}/').status_code, 404)
        self.assertEqual(self.client.post('/api/v1/equipos/', {'nombre': 'X'}).status_code, 401)

    def test_flujo_admin_fixture_y_confirmacion(self):
        self.client.force_authenticate(self.admin)
        camp = Campeonato.objects.create(nombre='Nuevo', publicado=True)
        for e in crear_equipos(5, prefijo='Nuevo'):
            Inscripcion.objects.create(campeonato=camp, equipo=e)
        r = self.client.get(f'/api/v1/campeonatos/{camp.id}/fixture-info/')
        self.assertTrue(r.data['hay_descanso'])
        r = self.client.post(f'/api/v1/campeonatos/{camp.id}/generar-fixture/', {})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.data['requiere_confirmacion'])
        r = self.client.post(f'/api/v1/campeonatos/{camp.id}/generar-fixture/', {'confirmar_descanso': True}, format='json')
        self.assertEqual(r.status_code, 201)
        r = self.client.get(f'/api/v1/campeonatos/{camp.id}/tabla/')
        self.assertEqual(len(r.data[0]['filas']), 5)

    def test_api_relampago_con_confirmacion(self):
        self.client.force_authenticate(self.admin)
        camp = Campeonato.objects.create(nombre='Copa', publicado=True)
        for e in crear_equipos(3, prefijo='Copa'):
            Inscripcion.objects.create(campeonato=camp, equipo=e)
        info = self.client.get(f'/api/v1/campeonatos/{camp.id}/fixture-info/').data
        self.assertEqual({m['valor'] for m in info['modalidades']}, {'IDA', 'IDA_VUELTA', 'RELAMPAGO'})
        url = f'/api/v1/campeonatos/{camp.id}/generar-fixture/'
        self.assertEqual(self.client.post(url, {'modalidad': 'RELAMPAGO'}, format='json').status_code, 409)
        r = self.client.post(url, {'modalidad': 'RELAMPAGO', 'confirmar_descanso': True}, format='json')
        self.assertEqual((r.status_code, r.data['partidos']), (201, 1))
        self.assertEqual(self.client.get(f'/api/v1/campeonatos/{camp.id}/tabla/').data, [])
        jugar(camp.partidos.get(), 2, 0)
        r = self.client.post(f'/api/v1/campeonatos/{camp.id}/avanzar-eliminatoria/', {}, format='json')
        self.assertEqual((r.status_code, r.data['fase']), (200, 'FINAL'))

    def test_operador_solo_partidos(self):
        self.client.force_authenticate(self.op)
        self.assertEqual(self.client.post('/api/v1/equipos/', {'nombre': 'X'}).status_code, 403)
        fixture.generar_fixture(self.camp)
        p = self.camp.partidos.first()
        r = self.client.post(f'/api/v1/partidos/{p.id}/cambiar-estado/', {'estado': 'INICIADO'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['estado'], 'INICIADO')

    def test_el_operador_gestiona_un_partido_como_lo_hace_la_app_movil(self):
        """Mismas peticiones que envía la app: estado, gol y tarjeta sin jugador, eliminar el gol, y lista con token."""
        self.client.force_authenticate(self.op)
        fixture.generar_fixture(self.camp)
        p = self.camp.partidos.first()
        post = lambda url, datos: self.client.post(url, datos, format='json')  # noqa: E731
        self.assertEqual(post(f'/api/v1/partidos/{p.id}/cambiar-estado/', {'estado': 'INICIADO'}).status_code, 200)
        gol = post('/api/v1/eventos/', {'partido': p.id, 'equipo': p.local_id, 'tipo': 'GOL', 'minuto': 23,
                                        'jugador': None, 'jugador_relevo': None, 'descripcion': ''})
        self.assertEqual(gol.status_code, 201)
        amarilla = post('/api/v1/eventos/', {'partido': p.id, 'equipo': p.visitante_id, 'tipo': 'AMARILLA', 'minuto': 30,
                                             'jugador': None, 'jugador_relevo': None, 'descripcion': ''})
        self.assertEqual(amarilla.status_code, 201)
        detalle = self.client.get(f'/api/v1/partidos/{p.id}/').data
        self.assertEqual((detalle['goles_local'], detalle['goles_visitante'], len(detalle['eventos'])), (1, 0, 2))
        self.assertEqual(self.client.delete(f"/api/v1/eventos/{gol.data['id']}/").status_code, 204)
        self.assertEqual(self.client.get(f'/api/v1/partidos/{p.id}/').data['goles_local'], 0)
        self.assertEqual(post(f'/api/v1/partidos/{p.id}/cambiar-estado/', {'estado': 'FINALIZADO'}).status_code, 200)
        # la lista de gestión con sesión incluye también campeonatos no publicados
        self.camp.publicado = False
        self.camp.save()
        self.assertIn(p.id, [x['id'] for x in self.client.get('/api/v1/partidos/').data['results']])
        self.client.force_authenticate(None)
        self.assertNotIn(p.id, [x['id'] for x in self.client.get('/api/v1/partidos/').data['results']])


class LoginTolerarMayusculasTests(APITestCase):
    """La app móvil envía el usuario en minúsculas: el login no debe distinguir mayúsculas."""

    def setUp(self):
        User = get_user_model()
        self.u = User.objects.create_user('MAguilar', email='Mario@Siglo21.net', password='Clave-larga-1')

    def entrar(self, usuario, clave='Clave-larga-1'):
        return self.client.post('/api/v1/auth/login', {'username': usuario, 'password': clave}, format='json')

    def test_acepta_usuario_en_minusculas_y_correo(self):
        for forma in ('MAguilar', 'maguilar', 'MAGUILAR', ' maguilar ', 'mario@siglo21.net'):
            r = self.entrar(forma)
            self.assertEqual(r.status_code, 200, forma)
            self.assertEqual(r.data['usuario']['username'], 'MAguilar')

    def test_clave_incorrecta_sigue_fallando(self):
        self.assertEqual(self.entrar('maguilar', 'otra-clave').status_code, 401)
        self.assertEqual(self.entrar('nadie').status_code, 401)

    def test_si_hay_dos_usuarios_que_solo_difieren_en_mayusculas_gana_el_exacto(self):
        User = get_user_model()
        User.objects.create_user('maguilar', password='Otra-clave-2')
        self.assertEqual(self.entrar('maguilar', 'Otra-clave-2').data['usuario']['username'], 'maguilar')
        self.assertEqual(self.entrar('MAguilar').data['usuario']['username'], 'MAguilar')
        self.assertEqual(self.entrar('MAGUILAR').status_code, 401)   # ambiguo: no se adivina


class RemitenteDelCorreoTests(APITestCase):
    def test_remitente_con_solo_un_nombre_usa_el_usuario_smtp(self):
        from torneos.services.correo import _remitente
        cfg = ConfiguracionSistema(correo_remitente='GoalScore', smtp_usuario='goalscore@acepsistemas.com')
        self.assertEqual(_remitente(cfg), 'GoalScore <goalscore@acepsistemas.com>')
        # una dirección completa se respeta tal cual
        cfg.correo_remitente = 'GoalScore <avisos@acepsistemas.com>'
        self.assertEqual(_remitente(cfg), 'GoalScore <avisos@acepsistemas.com>')
        cfg.correo_remitente = 'avisos@acepsistemas.com'
        self.assertEqual(_remitente(cfg), 'avisos@acepsistemas.com')
        # sin usuario con formato de correo no hay de dónde sacar la dirección
        cfg.correo_remitente, cfg.smtp_usuario = 'GoalScore', 'usuario_sin_arroba'
        self.assertEqual(_remitente(cfg), 'GoalScore')


class FiltroEquiposPorCampeonatoTests(APITestCase):
    def test_filtra_los_equipos_inscritos_en_un_campeonato(self):
        admin = get_user_model().objects.create_superuser('filtro_admin', password='Clave-larga-1')
        self.client.force_authenticate(admin)
        c1 = Campeonato.objects.create(nombre='Copa A')
        c2 = Campeonato.objects.create(nombre='Copa B')
        a, b, c = (Equipo.objects.create(nombre=n) for n in ('Ala', 'Beta', 'Gama'))
        for camp, eq in ((c1, a), (c1, b), (c2, b), (c2, c)):
            Inscripcion.objects.create(campeonato=camp, equipo=eq)

        def nombres(**filtro):
            r = self.client.get('/api/v1/equipos/', filtro)
            return sorted(e['nombre'] for e in r.data['results'])

        self.assertEqual(nombres(), ['Ala', 'Beta', 'Gama'])
        self.assertEqual(nombres(campeonato=c1.id), ['Ala', 'Beta'])
        self.assertEqual(nombres(campeonato=c2.id), ['Beta', 'Gama'])   # Beta está en los dos y no se repite
        self.assertEqual(nombres(campeonato=999999), [])
        self.assertEqual(nombres(campeonato='no-numero'), ['Ala', 'Beta', 'Gama'])


class SegundoTiempoTests(APITestCase):
    """Primer tiempo -> entretiempo -> segundo tiempo -> final, con la hora de cada fase y el tiempo de cada evento."""

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('seg_admin', password='Clave-larga-1')
        self.client.force_authenticate(self.admin)
        self.camp = crear_campeonato(2, prefijo='Seg', duracion_tiempo=20)
        fixture.generar_fixture(self.camp)
        self.p = self.camp.partidos.get()

    def estado(self, estado):
        r = self.client.post(f'/api/v1/partidos/{self.p.id}/cambiar-estado/', {'estado': estado}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def evento(self, minuto, **extra):
        r = self.client.post('/api/v1/eventos/', {'partido': self.p.id, 'equipo': self.p.local_id, 'tipo': 'GOL',
                                                  'minuto': minuto, **extra}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_recorrido_completo_con_horas_y_tiempo_de_cada_evento(self):
        self.assertEqual(self.estado('INICIADO')['estado_nombre'], 'Primer tiempo')
        self.assertEqual(self.evento(10)['tiempo'], 1)
        self.estado('ENTRETIEMPO')
        self.assertEqual(self.evento(21)['tiempo'], 1)            # durante el entretiempo sigue siendo del primero
        d = self.estado('SEGUNDO_TIEMPO')
        self.assertEqual(d['estado_nombre'], 'Segundo tiempo')
        self.assertEqual(self.evento(30)['tiempo'], 2)
        self.assertEqual(self.evento(35, tiempo=1)['tiempo'], 1)  # lo explícito se respeta
        en_vivo = self.client.get('/api/v1/partidos/en-vivo/').data
        self.assertEqual([x['id'] for x in en_vivo], [self.p.id])  # el segundo tiempo también es «en vivo»
        self.estado('FINALIZADO')
        det = self.client.get(f'/api/v1/partidos/{self.p.id}/').data
        for campo in ('inicio_at', 'fin_primer_tiempo_at', 'inicio_segundo_tiempo_at', 'fin_at'):
            self.assertIsNotNone(det[campo], campo)
        self.assertLessEqual(det['inicio_at'], det['fin_primer_tiempo_at'])
        self.assertLessEqual(det['fin_primer_tiempo_at'], det['inicio_segundo_tiempo_at'])
        self.assertLessEqual(det['inicio_segundo_tiempo_at'], det['fin_at'])
        self.assertEqual([(e['minuto'], e['tiempo']) for e in det['eventos']], [(10, 1), (21, 1), (35, 1), (30, 2)])
        textos = ' | '.join(Notificacion.objects.filter(partido=self.p, tipo='ESTADO').values_list('cuerpo', flat=True))
        self.assertIn('Comenzó el segundo tiempo', textos)

    def test_en_un_partido_terminado_el_minuto_decide_el_tiempo(self):
        self.estado('FINALIZADO')
        self.assertEqual(self.evento(15)['tiempo'], 1)
        self.assertEqual(self.evento(33)['tiempo'], 2)            # pasa de los 20 min del primer tiempo

    def test_volver_a_pendiente_reinicia_las_horas_pero_reabrir_conserva_el_inicio(self):
        self.estado('INICIADO')
        inicio = self.client.get(f'/api/v1/partidos/{self.p.id}/').data['inicio_at']
        self.estado('FINALIZADO')
        self.estado('INICIADO')    # reabrir
        self.assertEqual(self.client.get(f'/api/v1/partidos/{self.p.id}/').data['inicio_at'], inicio)
        d = self.estado('PENDIENTE')
        self.assertEqual([d[c] for c in ('inicio_at', 'fin_primer_tiempo_at', 'inicio_segundo_tiempo_at', 'fin_at')], [None] * 4)

    def test_el_segundo_tiempo_bloquea_repetir_el_sorteo(self):
        self.estado('SEGUNDO_TIEMPO')
        self.assertIsNotNone(fixture.sorteo_bloqueado(self.camp))


class DuracionDeTiempoTests(APITestCase):
    """Duración de cada tiempo: se guarda con el campeonato y NO cambia la separación entre partidos."""

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('dur_admin', password='Clave-larga-1')
        self.client.force_authenticate(self.admin)
        self.camp = Campeonato.objects.create(nombre='Con duración')
        for e in crear_equipos(4, prefijo='Dur'):
            Inscripcion.objects.create(campeonato=self.camp, equipo=e)
        self.url = f'/api/v1/campeonatos/{self.camp.id}/generar-fixture/'

    def test_por_defecto_son_45_minutos(self):
        self.assertEqual(self.camp.duracion_tiempo, 45)
        self.assertEqual(self.client.get(f'/api/v1/campeonatos/{self.camp.id}/').data['duracion_tiempo'], 45)

    def test_se_guarda_con_el_sorteo_y_no_cambia_los_horarios(self):
        r = self.client.post(self.url, {'modalidad': 'IDA', 'fecha_inicio': '2026-11-02', 'dias_semana': [1],
                                        'partidos_por_dia': 2, 'hora_inicio': '19:00', 'minutos_entre_partidos': 90,
                                        'duracion_tiempo': 25}, format='json')
        self.assertEqual(r.status_code, 201)
        self.camp.refresh_from_db()
        self.assertEqual((self.camp.duracion_tiempo, self.camp.minutos_entre_partidos), (25, 90))
        primera, segunda = self.camp.partidos.filter(fecha__numero=1).order_by('fecha_hora')[:2]
        self.assertEqual((segunda.fecha_hora - primera.fecha_hora).total_seconds() / 60, 90)   # sigue siendo la separación
        info = self.client.get(f'/api/v1/campeonatos/{self.camp.id}/fixture-info/').data['programacion']
        self.assertEqual(info['duracion_tiempo'], 25)
        self.assertEqual(self.client.get(f'/api/v1/partidos/{primera.id}/').data['duracion_tiempo'], 25)

    def test_valida_los_minutos(self):
        for malo in (500, 'abc'):
            r = self.client.post(self.url, {'modalidad': 'IDA', 'duracion_tiempo': malo}, format='json')
            self.assertEqual(r.status_code, 400, malo)
        self.assertEqual(self.client.patch(f'/api/v1/campeonatos/{self.camp.id}/', {'duracion_tiempo': 0}, format='json').status_code, 400)
        ok = self.client.patch(f'/api/v1/campeonatos/{self.camp.id}/', {'duracion_tiempo': 20}, format='json')
        self.assertEqual((ok.status_code, ok.data['duracion_tiempo']), (200, 20))
