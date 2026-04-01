"""
WSGI config for corun-ai subpath deployment.

Used when the app is mounted at a subpath (e.g. /doc) behind Apache.
Sets SCRIPT_NAME so Django generates correct URLs.
"""

import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'corun_project.settings')


class ScriptNameFix:
    def __init__(self, app, script_name):
        self.app = app
        self.script_name = script_name

    def __call__(self, environ, start_response):
        environ['SCRIPT_NAME'] = self.script_name
        path_info = environ.get('PATH_INFO', '')
        if path_info.startswith(self.script_name):
            environ['PATH_INFO'] = path_info[len(self.script_name):]
        return self.app(environ, start_response)


application = ScriptNameFix(get_wsgi_application(), '/doc')
