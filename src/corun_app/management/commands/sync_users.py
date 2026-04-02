"""Sync user accounts from swf-remote (epic-devcloud /prod/).

Reads directly from swf-remote's PostgreSQL auth_user table on localhost
and creates/updates matching accounts in corun's database. Password hashes
are copied directly so users have the same credentials.

Usage:
    python manage.py sync_users
"""

import logging
import os

import psycopg
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

SWF_REMOTE_DB = {
    'dbname': os.environ.get('SYNC_SOURCE_DB_NAME', 'swf_remote'),
    'user': os.environ.get('SYNC_SOURCE_DB_USER', 'swf_remote'),
    'password': os.environ.get('SYNC_SOURCE_DB_PASSWORD', ''),
    'host': os.environ.get('SYNC_SOURCE_DB_HOST', 'localhost'),
    'port': int(os.environ.get('SYNC_SOURCE_DB_PORT', '5432')),
}


class Command(BaseCommand):
    help = 'Sync user accounts from swf-remote'

    def handle(self, *args, **options):
        try:
            with psycopg.connect(**SWF_REMOTE_DB) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT username, password, is_active, is_staff, is_superuser "
                        "FROM auth_user WHERE is_active = true"
                    )
                    upstream_users = cur.fetchall()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to connect to swf-remote DB: {e}"))
            return

        if not upstream_users:
            self.stdout.write('No active users in swf-remote.')
            return

        User = get_user_model()
        created = updated = unchanged = 0

        for username, pw_hash, is_active, is_staff, is_superuser in upstream_users:
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    'is_active': is_active,
                    'is_staff': is_staff,
                    'is_superuser': is_superuser,
                },
            )
            if was_created:
                user.password = pw_hash
                user.save(update_fields=['password'])
                created += 1
                self.stdout.write(self.style.SUCCESS(f'  Created: {username}'))
            else:
                unchanged += 1

        self.stdout.write(
            f'Done. {created} created, {updated} updated, '
            f'{unchanged} unchanged (of {len(upstream_users)} from swf-remote).'
        )
