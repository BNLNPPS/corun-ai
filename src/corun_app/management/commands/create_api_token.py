"""
Management command: create_api_token

Usage:
    python manage.py create_api_token <username>

Prints the token key to stdout. Idempotent — if a token already exists
for this user it is returned unchanged; pass --rotate to regenerate it.
"""

import traceback

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = 'Mint (or retrieve) a DRF bearer token for a service account user.'

    def add_arguments(self, parser):
        parser.add_argument('username', help='Username of the service account')
        parser.add_argument(
            '--rotate',
            action='store_true',
            help='Delete and regenerate the token instead of returning the existing one',
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options['username']

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'User "{username}" does not exist.')

        try:
            if options['rotate']:
                Token.objects.filter(user=user).delete()
                token = Token.objects.create(user=user)
                self.stdout.write(self.style.WARNING(f'Token rotated for user "{username}".'))
            else:
                token, created = Token.objects.get_or_create(user=user)
                if created:
                    self.stdout.write(self.style.WARNING(f'New token created for user "{username}".'))
                else:
                    self.stdout.write(self.style.WARNING(f'Existing token returned for user "{username}".'))

            self.stdout.write(token.key)

        except Exception:
            traceback.print_exc()
            raise CommandError(f'Failed to create/retrieve token for "{username}".')
