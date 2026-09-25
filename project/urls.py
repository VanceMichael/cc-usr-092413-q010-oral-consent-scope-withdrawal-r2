from django.urls import path
from django.http import JsonResponse

from consent import views


urlpatterns = [
    path("healthz", lambda request: JsonResponse({"status": "ok"})),
    path("materials", views.materials),
    path("consents", views.consents),
    path("consents/<int:consent_id>/<str:action>", views.consent_detail),
    path("consents/narrow", views.narrow),
    path("requests", views.requests_view),
    path("requests/<str:request_key>", views.request_detail),
    path("requests/<str:request_key>/claim", views.claim),
    path("grants/<int:grant_id>/complete", views.grant_complete),
    path("explain", views.explain),
    path("jobs/<int:job_id>/run", views.jobs_run),
    path("jobs/<int:job_id>", views.job_detail),
]
