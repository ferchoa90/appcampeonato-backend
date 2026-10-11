"""Consulta de feriados aplicables a un campeonato (globales y propios, con repetición anual)."""
from django.db.models import Q

from torneos.models import Feriado


class CalendarioFeriados:
    def __init__(self, campeonato=None):
        filtro = Q(campeonato__isnull=True)
        if campeonato is not None:
            filtro |= Q(campeonato=campeonato)
        self._exactos, self._anuales = {}, {}
        for f in Feriado.objects.filter(filtro):
            if f.se_repite:
                self._anuales[(f.fecha.month, f.fecha.day)] = f.nombre
            else:
                self._exactos[f.fecha] = f.nombre

    def nombre(self, dia):
        """Nombre del feriado de `dia` (date) o None si es un día normal."""
        return self._exactos.get(dia) or self._anuales.get((dia.month, dia.day))
