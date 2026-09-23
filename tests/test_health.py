import os
import unittest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

import django

django.setup()

from django.test import Client


class HealthTest(unittest.TestCase):
    def test_health_endpoint(self) -> None:
        response = Client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
