"""HTTP 接口：薄封装，领域规则全部在 services/jobs。"""
from __future__ import annotations

import json
from datetime import date

from django.http import JsonResponse, HttpResponseNotAllowed, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt

from . import models as m
from . import services as s
from . import jobs as j


def _body(request):
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def _actor(request):
    code = request.headers.get("X-Actor-Code")
    if not code:
        return None
    return m.Actor.objects.filter(code=code).first()


def _require_actor(request):
    actor = _actor(request)
    if actor is None:
        return None, JsonResponse({"error": "actor_required"}, status=401)
    return actor, None


def _parse_date(value):
    return date.fromisoformat(value) if value else None


def _error(exc):
    return JsonResponse({"error": str(exc)}, status=422)


@csrf_exempt
def materials(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    body = _body(request)
    parent = None
    if body.get("parent_code"):
        parent = (m.Material.objects
                  .filter(code=body["parent_code"],
                          content_version=body.get("parent_version"))
                  .first() if body.get("parent_version") else
                  m.Material.objects.filter(code=body["parent_code"])
                  .order_by("-content_version").first())
        if parent is None:
            return HttpResponseBadRequest("parent_not_found")
    try:
        mat = s.register_material(
            actor=actor, code=body["code"], kind=body["kind"],
            content_fingerprint=body["fingerprint"],
            narrator_code=body["narrator_code"],
            session_code=body["session_code"],
            recorded_date=_parse_date(body["recorded_date"]),
            parent=parent, source_purpose=body.get("source_purpose"))
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"code": mat.code, "content_version": mat.content_version,
                         "source_consent_version":
                         (mat.source_consent.version if mat.source_consent else None)},
                        status=201)


@csrf_exempt
def consents(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    b = _body(request)
    try:
        consent = s.submit_consent(
            actor=actor, narrator_code=b["narrator_code"],
            session_code=b["session_code"], purpose=b["purpose"],
            audience=b["audience"], region=b["region"],
            valid_from=_parse_date(b["valid_from"]),
            valid_until=_parse_date(b["valid_until"]),
            attribution=b["attribution"], evidence_summary=b["evidence_summary"],
            material_codes=b["material_codes"])
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"chain_id": consent.chain_id, "version": consent.version,
                         "id": consent.id, "status": consent.status}, status=201)


def _consent_action(request, consent_id, action):
    actor, err = _require_actor(request)
    if err:
        return err
    consent = m.Consent.objects.filter(pk=consent_id).first()
    if consent is None:
        return JsonResponse({"error": "consent_not_found"}, status=404)
    try:
        if action == "confirm":
            body = {}
            if request.body:
                body = _body(request)
            consent = s.confirm_consent(consent, actor,
                                        restrictions=body.get("restrictions"))
        elif action == "open":
            consent = s.open_consent(consent, actor)
        elif action == "revoke":
            s.revoke_consent(consent, actor)
            consent.refresh_from_db()
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"id": consent.id, "version": consent.version,
                         "status": consent.status})


@csrf_exempt
def consent_detail(request, consent_id, action):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return _consent_action(request, consent_id, action)


@csrf_exempt
def narrow(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    b = _body(request)
    prev = m.Consent.objects.filter(pk=b["consent_id"]).first()
    if prev is None:
        return JsonResponse({"error": "consent_not_found"}, status=404)
    try:
        new = s.narrow_consent(
            prev, actor, audience=b.get("audience"), region=b.get("region"),
            valid_until=_parse_date(b["valid_until"]) if b.get("valid_until") else None,
            kept_codes=b["kept_codes"])
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"id": new.id, "chain_id": new.chain_id,
                         "version": new.version}, status=201)


@csrf_exempt
def requests_view(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    b = _body(request)
    try:
        req, reused = s.submit_request(
            actor=actor, request_key=b["request_key"], purpose=b["purpose"],
            audience=b["audience"], region=b["region"],
            on_date=_parse_date(b.get("on_date") or date.today().isoformat()),
            requested_codes=b["requested_codes"])
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({
        "request_key": req.request_key,
        "status": req.status,
        "reused": reused,
        "reuse_count": req.reuse_count,
        "manual_review_reason": req.manual_review_reason or None,
        "manifest": s.visible_manifest(req),
        "manifest_version": req.manifest_version or None,
        "items": (req.decision or {}).get("items", []),
    })


def request_detail(request, request_key):
    actor, err = _require_actor(request)
    if err:
        return err
    req = m.AccessRequest.objects.filter(request_key=request_key).first()
    if req is None:
        return JsonResponse({"error": "request_not_found"}, status=404)
    return JsonResponse({
        "request_key": req.request_key, "status": req.status,
        "reuse_count": req.reuse_count,
        "manual_review_reason": req.manual_review_reason or None,
        "manifest": s.visible_manifest(req),
        "manifest_version": req.manifest_version or None,
    })


@csrf_exempt
def claim(request, request_key):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    req = m.AccessRequest.objects.filter(request_key=request_key).first()
    if req is None:
        return JsonResponse({"error": "request_not_found"}, status=404)
    try:
        credentials = s.claim(req, actor)
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"manifest_version": req.manifest_version,
                         "credentials": credentials})


@csrf_exempt
def grant_complete(request, grant_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    actor, err = _require_actor(request)
    if err:
        return err
    grant = m.Grant.objects.filter(pk=grant_id).first()
    if grant is None:
        return JsonResponse({"error": "grant_not_found"}, status=404)
    s.complete_grant(grant, actor)
    grant.refresh_from_db()
    return JsonResponse({"id": grant.id, "state": grant.state})


def explain(request):
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        rows = s.explain(
            actor,
            purpose=request.GET.get("purpose", ""),
            code=request.GET.get("code") or None,
            date_from=_parse_date(request.GET.get("date_from")) if request.GET.get("date_from") else None,
            date_to=_parse_date(request.GET.get("date_to")) if request.GET.get("date_to") else None,
            audience=request.GET.get("audience") or None,
            region=request.GET.get("region") or None,
            on_date=_parse_date(request.GET["on_date"]) if request.GET.get("on_date") else None)
    except s.DomainError as exc:
        return _error(exc)
    return JsonResponse({"rows": rows})


@csrf_exempt
def jobs_run(request, job_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    job = m.Job.objects.filter(pk=job_id).first()
    if job is None:
        return JsonResponse({"error": "job_not_found"}, status=404)
    try:
        job = j.run_job(job_id)
    except j.JobInterrupted:
        return JsonResponse({"id": job.id, "status": m.JOB_PENDING,
                             "interrupted": True, "cursor": job.cursor})
    return JsonResponse({"id": job.id, "status": job.status, "cursor": job.cursor,
                         "runs": job.runs})


def job_detail(request, job_id):
    job = m.Job.objects.filter(pk=job_id).first()
    if job is None:
        return JsonResponse({"error": "job_not_found"}, status=404)
    return JsonResponse({"id": job.id, "kind": job.kind, "status": job.status,
                         "cursor": job.cursor, "runs": job.runs,
                         "last_error": job.last_error})
