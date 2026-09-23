SECRET_KEY = 'local-only'
DEBUG = True
ROOT_URLCONF = 'project.urls'
INSTALLED_APPS = ['django.contrib.contenttypes', 'rest_framework', 'consent']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'data.sqlite3'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

