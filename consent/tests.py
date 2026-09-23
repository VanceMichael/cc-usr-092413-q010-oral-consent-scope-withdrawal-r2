from django.test import SimpleTestCase
class HealthTest(SimpleTestCase):
    def test_health(self):
        self.assertEqual(1, 1)

