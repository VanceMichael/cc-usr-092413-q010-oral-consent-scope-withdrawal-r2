from django.urls import path
from django.http import JsonResponse
urlpatterns = [path('healthz', lambda request: JsonResponse({'status': 'ok'}))]

