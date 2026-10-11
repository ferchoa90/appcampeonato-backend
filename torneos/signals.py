from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from torneos.models import EventoPartido, Partido, PartidoMedio


@receiver(post_save, sender=Partido)
def partido_guardado(sender, instance, created, **kwargs):
    if kwargs.get('raw'):  # carga de datos (loaddata): no se notifica
        return
    from torneos.services.notificaciones import notificar_cambio_estado, notificar_marcador
    previo = instance._estado_previo
    marcador_previo = instance._marcador_previo
    instance._estado_previo = instance.estado
    instance._marcador_previo = (instance.goles_local, instance.goles_visitante)
    if created or previo is None:
        return
    if previo != instance.estado:
        notificar_cambio_estado(instance)  # el aviso de estado ya incluye el marcador
    elif marcador_previo and marcador_previo != (instance.goles_local, instance.goles_visitante):
        notificar_marcador(instance)       # corrección manual del marcador (los goles por evento avisan por su evento)


def recalcular_marcador(partido_id):
    """El marcador se deriva de los eventos de gol (autogol suma al rival)."""
    partido = Partido.objects.get(pk=partido_id)
    goles = {partido.local_id: 0, partido.visitante_id: 0}
    for ev in EventoPartido.objects.filter(partido=partido, tipo__in=EventoPartido.GOLES):
        beneficiado = ev.equipo_id
        if ev.tipo == 'AUTOGOL':
            beneficiado = partido.visitante_id if ev.equipo_id == partido.local_id else partido.local_id
        goles[beneficiado] = goles.get(beneficiado, 0) + 1
    Partido.objects.filter(pk=partido.pk).update(
        goles_local=goles[partido.local_id], goles_visitante=goles[partido.visitante_id])


@receiver(post_save, sender=EventoPartido)
def evento_guardado(sender, instance, created, **kwargs):
    if kwargs.get('raw'):  # carga de datos (loaddata): ni se recalcula ni se notifica
        return
    if instance.tipo in EventoPartido.GOLES and Partido.objects.filter(pk=instance.partido_id).exists():
        recalcular_marcador(instance.partido_id)
    if created:
        from torneos.services.notificaciones import notificar_evento
        partido = Partido.objects.select_related('campeonato', 'local', 'visitante').get(pk=instance.partido_id)
        # no se avisa de eventos cargados a posteriori en partidos ya cerrados o postergados
        if partido.estado not in (Partido.Estado.FINALIZADO, Partido.Estado.POSTERGADO):
            instance.partido = partido  # con el marcador ya recalculado
            notificar_evento(instance)


@receiver(post_delete, sender=EventoPartido)
def evento_borrado(sender, instance, **kwargs):
    if instance.tipo in EventoPartido.GOLES and Partido.objects.filter(pk=instance.partido_id).exists():
        recalcular_marcador(instance.partido_id)


@receiver(post_delete, sender=PartidoMedio)
def borrar_archivo_del_medio(sender, instance, **kwargs):
    """Al eliminar un medio se borra también su archivo del disco."""
    if instance.archivo:
        instance.archivo.delete(save=False)
