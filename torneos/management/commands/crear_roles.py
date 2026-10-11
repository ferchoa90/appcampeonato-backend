from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from torneos.permissions import ADMIN, AFICIONADO, OPERADOR, ORGANIZADOR


class Command(BaseCommand):
    help = 'Crea los roles (Administrador, Organizador, Operador) y opcionalmente un usuario administrador.'

    def add_arguments(self, parser):
        parser.add_argument('--admin-user')
        parser.add_argument('--admin-password')

    def handle(self, *args, **opts):
        for nombre in (ADMIN, ORGANIZADOR, OPERADOR, AFICIONADO):
            Group.objects.get_or_create(name=nombre)
        self.stdout.write('Roles listos.')
        if opts['admin_user'] and opts['admin_password']:
            User = get_user_model()
            user, creado = User.objects.get_or_create(username=opts['admin_user'])
            user.set_password(opts['admin_password'])
            user.is_staff = user.is_superuser = True
            user.save()
            user.groups.add(Group.objects.get(name=ADMIN))
            self.stdout.write(f'Usuario administrador {"creado" if creado else "actualizado"}: {user.username}')
