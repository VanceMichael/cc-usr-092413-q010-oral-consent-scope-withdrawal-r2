"""
测试引导：切换到临时 SQLite 文件并执行迁移，
使 `python3 -m unittest discover -s tests` 与 `manage.py test` 都可运行。
（内存库 :memory: 在 Django 连接关闭后即丢失，故使用临时文件。）
"""
import os
import tempfile

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

import django
from django.conf import settings

if not settings.configured:
    django.setup()

_fd, _path = tempfile.mkstemp(prefix="consent-test-", suffix=".sqlite3")
os.close(_fd)
settings.DATABASES["default"]["NAME"] = _path

from django.core.management import call_command

call_command("migrate", run_syncdb=True, verbosity=0)
