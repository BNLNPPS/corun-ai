"""
Django settings for corun-ai — collaborative AI runner.

First deployment: code documentation for ePIC at epic-devcloud.org/doc/
"""

from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('CORUN_SECRET_KEY')
DEBUG = config('CORUN_DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('CORUN_ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corun_app',
    'codoc_app',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'corun_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            BASE_DIR / 'templates',
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'codoc_app.context_processors.user_theme',
            ],
        },
    },
]

WSGI_APPLICATION = 'corun_project.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('CORUN_DB_NAME', default='corun'),
        'USER': config('CORUN_DB_USER', default='corun'),
        'PASSWORD': config('CORUN_DB_PASSWORD', default=''),
        'HOST': config('CORUN_DB_HOST', default='localhost'),
        'PORT': config('CORUN_DB_PORT', default='5432'),
    },
}

# tjai integration — Gemma jobs are dispatched via tjai's remote-worker pipeline
# (the worker daemon POSTs to /api/work/submit and polls /api/work/result/<uuid>).
# tjai runs on the same host so the default localhost URL is correct;
# override via CORUN_TJAI_BASE_URL if tjai is moved.
TJAI_BASE_URL = config('CORUN_TJAI_BASE_URL', default='http://127.0.0.1:8002')

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = True

# Subpath deployment (e.g. /doc on epic-devcloud.org)
FORCE_SCRIPT_NAME = config('CORUN_FORCE_SCRIPT_NAME', default='') or None

STATIC_URL = config('CORUN_STATIC_URL', default='/static/')
STATIC_ROOT = BASE_DIR.parent / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Cookie scoping — unique names prevent conflicts with other apps on same domain
_subpath = FORCE_SCRIPT_NAME or ""
CSRF_COOKIE_PATH = _subpath or "/"
SESSION_COOKIE_PATH = _subpath or "/"
CSRF_COOKIE_NAME = 'csrftoken_doc'
SESSION_COOKIE_NAME = 'sessionid_doc'

# Behind Apache reverse proxy
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Authentication
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'codoc:home'
LOGOUT_REDIRECT_URL = 'codoc:home'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'worker': {
            'format': '%(asctime)s [%(levelname)s] %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'worker',
        },
    },
    'loggers': {
        'corun.worker': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}

