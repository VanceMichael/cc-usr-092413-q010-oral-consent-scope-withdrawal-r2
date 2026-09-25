"""
领域服务：同意范围、最小可见清单、职责分离、撤回/收窄、并发领取、可解释查询。

关键约定
- 同意按 (讲述人, 采集场次, 用途) 形成链 chain，每次变化产生新版本；
- 材料的可见性由 ConsentScope（某同意版本是否覆盖该材料）决定，
  派生材料通过 derived_from（具体父本版本）与 source_consent（来源范围快照）留存谱系；
- 申请决定绑定 manifest_version，领取凭据时在同一同意版本上复核，保证清单与凭据一致；
- 撤回/收窄只改状态并走下架-通知-回执流程，不删除任何审计事实。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone as dt_timezone

from django.db import transaction

from . import models as m


class DomainError(Exception):
    """领域规则违反。"""


def now() -> datetime:
    return datetime.now(dt_timezone.utc)


# ---------------------------------------------------------------- 审计

def _audit(actor, action: str, target: str = "", detail=None):
    m.AuditEvent.objects.create(
        actor=actor, action=action, target=target, detail=detail or {}
    )


def _require_role(actor, role: str):
    if not actor or not actor.has_role(role):
        raise DomainError(f"actor_{role}_required")


def _require_distinct(a, b, code: str):
    if a is not None and b is not None and a.pk == b.pk:
        raise DomainError(code)


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 材料登记

def register_material(*, actor, code, kind, content_fingerprint, narrator_code,
                      session_code, recorded_date, parent=None, source_purpose=None):
    """
    登记材料；同一 code 再登记即为新的内容版本（版本关系）。
    派生材料须指向具体父本内容版本，并保存来源范围（父本当时依据的同意版本）。
    """
    _require_role(actor, m.ROLE_ARCHIVIST)
    last = (
        m.Material.objects.filter(code=code)
        .order_by("-content_version")
        .first()
    )
    if last is None:
        version = 1
    else:
        version = last.content_version + 1
        if parent is None:
            parent = last  # 同一标识的新内容版本默认承继上一版本
        elif parent.code != code:
            raise DomainError("new_version_parent_must_share_code")
    source_consent = None
    if parent is not None:
        if not isinstance(parent, m.Material):
            raise DomainError("parent_must_be_version")
        source_consent = _source_consent(parent, source_purpose)
    material = m.Material.objects.create(
        code=code,
        content_version=version,
        fingerprint=content_fingerprint,
        kind=kind,
        narrator_code=narrator_code,
        session_code=session_code,
        recorded_date=recorded_date,
        derived_from=parent if parent is not None else None,
        source_consent=source_consent,
        submitted_by=actor,
        status=m.MAT_REGISTERED,
    )
    _audit(actor, "material_registered", code,
           {"version": version, "kind": kind,
            "parent": None if parent is None else f"{parent.code}@v{parent.content_version}",
            "source_consent": None if source_consent is None else source_consent.pk})
    return material


def _source_consent(parent: m.Material, purpose):
    """父本在派生时刻的来源范围：优先取指定用途的开放覆盖，否则取最近开放覆盖。"""
    scopes = (
        m.ConsentScope.objects.filter(
            material=parent, state="open", consent__status=m.CONSENT_OPEN
        )
        .select_related("consent")
        .order_by("-consent__version")
    )
    if purpose:
        scopes = scopes.filter(consent__purpose=purpose)
    scope = scopes.first()
    return scope.consent if scope else parent.source_consent


def latest_versions(codes):
    """按标识取当前最新内容版本，返回 {code: Material}；未知标识收集起来。"""
    result, unknown = {}, []
    for code in codes:
        mat = m.Material.objects.filter(code=code).order_by("-content_version").first()
        if mat is None:
            unknown.append(code)
        else:
            result[code] = mat
    return result, unknown


# ---------------------------------------------------------------- 同意生命周期

def submit_consent(*, actor, narrator_code, session_code, purpose, audience, region,
                   valid_from, valid_until, attribution, evidence_summary, material_codes):
    """档案员整理范围并提交同意：八字段一次固定，范围指向具体材料。"""
    _require_role(actor, m.ROLE_ARCHIVIST)
    materials, unknown = latest_versions(material_codes)
    if unknown:
        raise DomainError("unknown_materials:" + ",".join(unknown))

    chain_id = f"{narrator_code}|{session_code}|{purpose}"
    with transaction.atomic():
        locked = list(
            m.Consent.objects.select_for_update().filter(chain_id=chain_id)
        )
        if any(c.status in (m.CONSENT_DRAFT, m.CONSENT_SUBMITTED) for c in locked):
            raise DomainError("pending_consent_exists")
        version = (max(c.version for c in locked) + 1) if locked else 1
        supersedes = max(locked, key=lambda c: c.version) if locked else None
        consent = m.Consent.objects.create(
            chain_id=chain_id, version=version,
            narrator_code=narrator_code, session_code=session_code, purpose=purpose,
            audience=list(audience), region=list(region),
            valid_from=valid_from, valid_until=valid_until,
            attribution=attribution, evidence_summary=evidence_summary,
            status=m.CONSENT_SUBMITTED, supersedes=supersedes, submitted_by=actor,
        )
        for code, material in materials.items():
            m.ConsentScope.objects.create(consent=consent, material=material,
                                          state="submitted")
            if material.submitted_by_id is None:
                material.submitted_by = actor
                material.save(update_fields=["submitted_by"])
        _audit(actor, "consent_submitted", chain_id,
               {"version": version, "materials": list(materials.keys())})
    return consent


def confirm_consent(consent, actor, restrictions=None):
    """伦理复核人确认限制；不得确认自己提交的同意。"""
    _require_role(actor, m.ROLE_ETHICS)
    restrictions = restrictions or {}
    with transaction.atomic():
        consent = m.Consent.objects.select_for_update().get(pk=consent.pk)
        if consent.status != m.CONSENT_SUBMITTED:
            raise DomainError("consent_not_submittable")
        _require_distinct(actor, consent.submitted_by, "cannot_confirm_own_submission")
        consent.status = m.CONSENT_CONFIRMED
        consent.confirmed_by = actor
        consent.confirmed_at = now()
        consent.save()
        for scope in consent.scopes.select_related("material"):
            # 任何人都不能确认自己提交的节选等材料
            _require_distinct(actor, scope.material.submitted_by,
                              "cannot_confirm_own_excerpt")
            scope.state = "ready"
            note = restrictions.get(scope.material.code, "")
            if note:
                scope.material.restriction_note = note
                scope.material.confirmed_by = actor
                scope.material.status = m.MAT_READY
                scope.material.save(update_fields=["restriction_note",
                                                   "confirmed_by", "status"])
            else:
                if scope.material.status == m.MAT_REGISTERED:
                    scope.material.status = m.MAT_READY
                    scope.material.confirmed_by = actor
                    scope.material.save(update_fields=["status", "confirmed_by"])
            scope.save(update_fields=["state"])
        _audit(actor, "consent_confirmed", consent.chain_id,
               {"version": consent.version, "restrictions": restrictions})
    return consent


def open_consent(consent, actor):
    """发布人执行开放；不得发布自己提交或确认的内容，三段职责必须为不同的人。"""
    _require_role(actor, m.ROLE_PUBLISHER)
    with transaction.atomic():
        consent = m.Consent.objects.select_for_update().get(pk=consent.pk)
        if consent.status != m.CONSENT_CONFIRMED:
            raise DomainError("consent_not_confirmed")
        _require_distinct(actor, consent.submitted_by, "cannot_open_own_submission")
        _require_distinct(actor, consent.confirmed_by, "publisher_must_be_distinct")
        consent.status = m.CONSENT_OPEN
        consent.opened_by = actor
        consent.opened_at = now()
        consent.save()
        for scope in consent.scopes.select_related("material"):
            # 任何人都不能发布自己提交的节选等材料
            _require_distinct(actor, scope.material.submitted_by,
                              "cannot_open_own_excerpt")
            scope.state = "open"
            scope.save(update_fields=["state"])
            material = scope.material
            if material.status != m.MAT_OPEN:
                material.status = m.MAT_OPEN
                material.save(update_fields=["status"])
        _audit(actor, "consent_opened", consent.chain_id,
               {"version": consent.version})
        # 仅“范围收窄”新版本在开放同一事务内令旧版本让位，范围外覆盖停止并入下架
        if consent.is_narrow:
            _apply_narrow_effect_locked(consent, actor)
    return consent


# ---------------------------------------------------------------- 可见性决定

def _visible_scope(material, purpose, audience, region, on_date):
    """
    返回 (scope, reason)。scope 为该材料在给定用途/受众/地区/日期下
    唯一有效的开放覆盖（取链上最高版本），否则给出不可见原因。
    """
    scopes = list(
        m.ConsentScope.objects.filter(
            material=material, consent__purpose=purpose
        ).select_related("consent")
    )
    # 撤回/收窄移除优先：即便下架流程已完成，也应提示重新申请
    ceased = [s for s in scopes if s.state == "taken_down"
              or s.consent.status in (m.CONSENT_REVOKED, m.CONSENT_EXPIRED)]
    if ceased:
        latest_ceased = max(
            ceased, key=lambda s: (s.consent.version, s.consent.ended_at or s.consent.id))
        return None, ("revoked_reapply"
                      if latest_ceased.consent.status != m.CONSENT_EXPIRED
                      else "expired_reapply")
    if material.status not in (m.MAT_OPEN, m.MAT_READY, m.MAT_REGISTERED,
                               m.MAT_TAKE_DOWN_PENDING):
        return None, f"material_{material.status}"
    open_scopes = [s for s in scopes if s.state == "open"
                   and s.consent.status == m.CONSENT_OPEN]
    if not open_scopes:
        return None, "no_open_consent"
    valid = [s for s in open_scopes
             if s.consent.valid_from <= on_date <= s.consent.valid_until]
    if not valid:
        cur = max(open_scopes, key=lambda s: s.consent.version).consent
        return None, ("expired_reapply" if on_date > cur.valid_until
                      else "not_yet_valid")
    if audience is not None:
        valid = [s for s in valid if audience in s.consent.audience]
        if not valid:
            return None, "audience_not_covered"
    if region is not None:
        valid = [s for s in valid if region in s.consent.region]
        if not valid:
            return None, "region_not_covered"
    return max(valid, key=lambda s: s.consent.version), None


def _decision_items(materials, unknown, purpose, audience, region, on_date):
    items = []
    for code in unknown:
        items.append({
            "code": code,
            "content_version": None,
            "fingerprint": None,
            "visible": False,
            "reason": "material_not_registered",
            "consent_chain": None,
            "consent_version": None,
            "consent_pk": None,
        })
    for code, material in materials.items():
        scope, reason = _visible_scope(material, purpose, audience, region, on_date)
        items.append({
            "code": code,
            "content_version": material.content_version,
            "fingerprint": material.fingerprint,
            "visible": scope is not None,
            "reason": reason,
            "consent_chain": scope.consent.chain_id if scope else None,
            "consent_version": scope.consent.version if scope else None,
            "consent_pk": scope.consent.pk if scope else None,
        })
    return items


def _manifest(items):
    visible = sorted(
        ((i["code"], i["content_version"], i["fingerprint"],
          i["consent_chain"], i["consent_version"])
         for i in items if i["visible"]),
        key=lambda t: t[0],
    )
    return _fingerprint(visible), visible


# ---------------------------------------------------------------- 申请与幂等

def _flag_manual(actor, existing, request_key, reason):
    with transaction.atomic():
        ex = m.AccessRequest.objects.select_for_update().get(pk=existing.pk)
        if ex.status != m.REQ_MANUAL_REVIEW or ex.manual_review_reason != reason:
            ex.status = m.REQ_MANUAL_REVIEW
            ex.manual_review_reason = reason
            ex.decision = None
            ex.save()
            _audit(actor, "request_manual_review", request_key, {"reason": reason})


def submit_request(*, actor, request_key, purpose, audience, region, on_date,
                   requested_codes):
    """研究/展陈申请：同键同内容沿用原决定；同键异内容转人工核查。"""
    existing = m.AccessRequest.objects.filter(request_key=request_key).first()
    payload = [purpose, audience, region, on_date.isoformat(),
               sorted(requested_codes)]
    fp = _fingerprint(payload)
    if existing is not None:
        if existing.payload_fingerprint != fp:
            _flag_manual(actor, existing, request_key, "same_key_different_content")
            return m.AccessRequest.objects.get(pk=existing.pk), False
        # 申请参数相同，但所申请标识背后的材料内容已变（新版本/指纹变化）
        current_materials, _ = latest_versions(requested_codes)
        current_fps = {code: mat.fingerprint
                       for code, mat in current_materials.items()}
        if existing.status == m.REQ_DECIDED and \
                current_fps != (existing.fingerprint_snapshot or {}):
            _flag_manual(actor, existing, request_key,
                         "same_identifier_different_content")
            return m.AccessRequest.objects.get(pk=existing.pk), False
        existing.reuse_count += 1
        existing.save(update_fields=["reuse_count"])
        _audit(actor, "request_reused", request_key,
               {"reuse_count": existing.reuse_count})
        return existing, True

    materials, unknown = latest_versions(requested_codes)
    with transaction.atomic():
        req = m.AccessRequest.objects.create(
            request_key=request_key, applicant=actor, purpose=purpose,
            audience=audience, region=region, on_date=on_date,
            requested_codes=list(requested_codes), payload_fingerprint=fp,
        )
        items = _decision_items(materials, unknown, purpose, audience, region, on_date)
        manifest_version, visible = _manifest(items)
        req.decision = {"items": items}
        req.manifest_version = manifest_version
        req.fingerprint_snapshot = {i["code"]: i["fingerprint"] for i in items}
        req.consent_snapshot = {
            i["code"]: {"chain": i["consent_chain"], "version": i["consent_version"]}
            for i in items if i["visible"]
        }
        req.status = m.REQ_DECIDED
        req.decided_at = now()
        req.save()
        _audit(actor, "request_decided", request_key,
               {"visible": [v[0] for v in visible], "manifest": manifest_version[:12]})
    return req, False


def visible_manifest(req):
    """最小可见清单：只含与用途相符、当前可见的项与最少字段。"""
    return [{
        "code": i["code"],
        "content_version": i["content_version"],
        "consent_version": i["consent_version"],
    } for i in (req.decision or {}).get("items", []) if i["visible"]]


# ---------------------------------------------------------------- 领取（并发裁决）

def claim(req, actor):
    """
    下载领取：清单与文件凭据必须依据同一个同意版本。
    对相关同意链加锁后重算 manifest；若与决定时版本不同（撤回/收窄已落地），
    拒绝整批领取、要求重新申请，绝不发放部分凭据。
    """
    if actor.pk != req.applicant_id:
        raise DomainError("only_applicant_may_claim")
    with transaction.atomic():
        req = m.AccessRequest.objects.select_for_update().get(pk=req.pk)
        if req.status == m.REQ_MANUAL_REVIEW:
            raise DomainError("request_under_manual_review")

        chains = {snap["chain"] for snap in req.consent_snapshot.values()}
        # 按链名排序加锁，与撤回事务保持相同的锁序
        for chain_id in sorted(chains):
            list(m.Consent.objects.select_for_update().filter(chain_id=chain_id))

        materials, unknown = latest_versions(req.requested_codes)
        items = _decision_items(materials, unknown, req.purpose, req.audience,
                                req.region, req.on_date)
        current_manifest, _ = _manifest(items)
        if current_manifest != req.manifest_version:
            _audit(actor, "claim_rejected_version_changed", req.request_key,
                   {"decided": req.manifest_version[:12],
                    "current": current_manifest[:12]})
            raise DomainError("consent_version_changed_reapply")

        credentials = []
        for item in items:
            if not item["visible"]:
                continue
            material = materials[item["code"]]
            consent = m.Consent.objects.get(pk=item["consent_pk"])
            grant, created = m.Grant.objects.get_or_create(
                request=req, material=material,
                defaults={
                    "consent_version": consent,
                    "manifest_version": req.manifest_version,
                    "file_credential": _credential(req, material, consent),
                },
            )
            credentials.append({
                "code": material.code,
                "content_version": material.content_version,
                "credential": grant.file_credential,
                "consent_version": grant.consent_version.version,
                "state": grant.state,
            })
        _audit(actor, "credentials_issued", req.request_key,
               {"count": len(credentials), "manifest": req.manifest_version[:12]})
        return credentials


def _credential(req, material, consent):
    base = _canonical([req.request_key, material.code, material.content_version,
                       material.fingerprint, consent.chain_id, consent.version])
    return "file-" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def complete_grant(grant, actor):
    """阅览完成：成为审计事实，此后撤回也不删除。"""
    with transaction.atomic():
        grant = m.Grant.objects.select_for_update().get(pk=grant.pk)
        if grant.state == m.GRANT_ACTIVE:
            grant.state = m.GRANT_COMPLETED
            grant.completed_at = now()
            grant.save()
            _audit(actor, "access_completed", grant.file_credential,
                   {"code": grant.material.code})
    return grant


# ---------------------------------------------------------------- 撤回与收窄

def revoke_consent(consent, actor, reason="revoked"):
    """
    家属发起撤回：停止该讲述人/场次下全部用途的尚未完成访问与后续发布；
    已公开材料进入下架-通知-回执流程；已完成阅览仅保留审计事实。
    """
    _require_role(actor, m.ROLE_FAMILY)
    with transaction.atomic():
        chain_ids = sorted(
            m.Consent.objects.filter(
                narrator_code=consent.narrator_code,
                session_code=consent.session_code,
            ).values_list("chain_id", flat=True).distinct()
        )
        locked = []
        for chain_id in chain_ids:
            locked += list(m.Consent.objects.select_for_update()
                           .filter(chain_id=chain_id))
        ended = now()
        for c in locked:
            if c.status in (m.CONSENT_OPEN, m.CONSENT_CONFIRMED, m.CONSENT_SUBMITTED):
                c.status = m.CONSENT_REVOKED if reason == "revoked" else m.CONSENT_EXPIRED
                c.ended_at = ended
                c.save()
                _cease_scopes(c, actor, reason)
        _audit(actor, "consent_revoked",
               f"{consent.narrator_code}/{consent.session_code}",
               {"chains": chain_ids, "reason": reason})


def narrow_consent(previous, actor, *, audience=None, region=None, valid_until=None,
                   kept_codes):
    """
    范围收窄：档案员据新范围提交新版本，经伦理确认、发布后，
    不再纳入范围的旧覆盖即刻停止访问并入下架流程。
    返回新版本（仍须走 confirm/open）。
    """
    _require_role(actor, m.ROLE_ARCHIVIST)
    with transaction.atomic():
        prev = m.Consent.objects.select_for_update().get(pk=previous.pk)
        if prev.status not in (m.CONSENT_OPEN, m.CONSENT_SUPERSEDED):
            raise DomainError("can_only_narrow_open_consent")
        new = submit_consent(
            actor=actor, narrator_code=prev.narrator_code,
            session_code=prev.session_code, purpose=prev.purpose,
            audience=prev.audience if audience is None else audience,
            region=prev.region if region is None else region,
            valid_from=now().date(),
            valid_until=prev.valid_until if valid_until is None else valid_until,
            attribution=prev.attribution,
            evidence_summary=prev.evidence_summary + "；范围收窄",
            material_codes=kept_codes,
        )
        new.is_narrow = True
        new.save(update_fields=["is_narrow"])
        _audit(actor, "consent_narrow_drafted", prev.chain_id,
               {"from_version": prev.version, "kept": sorted(kept_codes)})
        return new


def expire_consent(consent, actor=None):
    """到期处置：与撤回同样停止未完成访问并对公开材料走下架流程。"""
    with transaction.atomic():
        c = m.Consent.objects.select_for_update().get(pk=consent.pk)
        if c.status != m.CONSENT_OPEN:
            return c
        c.status = m.CONSENT_EXPIRED
        c.ended_at = now()
        c.save()
        _cease_scopes(c, actor, "expired")
        _audit(actor, "consent_expired", c.chain_id, {"version": c.version})
    return c


def _apply_narrow_effect_locked(new_consent, actor):
    """新版本开放事务内：旧版本让位，范围之外的覆盖停止并下架。"""
    prev = new_consent.supersedes
    if prev is None:
        return
    kept = {s.material_id for s in new_consent.scopes.all()}
    removed = [s for s in prev.scopes.all() if s.material_id not in kept]
    if prev.status == m.CONSENT_OPEN:
        prev.status = m.CONSENT_SUPERSEDED
        prev.ended_at = now()
        prev.save()
        for s in prev.scopes.all():
            if s.state == "open" and s.material_id in kept:
                s.state = "superseded"
                s.save(update_fields=["state"])
        for scope in removed:
            _cease_scope(scope, actor, "narrowed")
        _carry_grants(prev, new_consent, kept)
    _audit(actor, "consent_narrow_applied", prev.chain_id,
           {"from_version": prev.version, "to_version": new_consent.version,
            "removed": [s.material.code for s in removed]})


def _carry_grants(old_consent, new_consent, kept_material_ids):
    """收窄后仍保留的材料：未完成访问继续有效，但盖戳迁移到新同意版本。"""
    for grant in m.Grant.objects.filter(
        consent_version=old_consent, state=m.GRANT_ACTIVE
    ).select_related("material"):
        if grant.material_id in kept_material_ids:
            grant.consent_version = new_consent
            grant.save(update_fields=["consent_version"])


def _cease_scopes(consent, actor, reason):
    for scope in consent.scopes.all():
        if scope.state in ("open", "ready", "submitted"):
            _cease_scope(scope, actor, reason)


def _cease_scope(scope, actor, reason):
    material = scope.material
    was_open = scope.state == "open"
    if was_open:
        order, created = m.TakedownOrder.objects.get_or_create(
            material=material, consent_version=scope.consent,
            reason=reason,
            defaults={"status": m.TD_PENDING},
        )
        scope.state = "taken_down"
        if created:
            from .jobs import enqueue_takedown
            enqueue_takedown(order.pk)
    else:
        scope.state = "blocked"
    scope.save()

    # 停止尚未完成的访问；已完成的阅览原样保留
    stopped = 0
    for grant in m.Grant.objects.filter(
        material=material, consent_version=scope.consent, state=m.GRANT_ACTIVE
    ):
        grant.state = m.GRANT_STOPPED
        grant.stopped_at = now()
        grant.save()
        stopped += 1
    _recompute_material(material)
    _audit(actor, "access_scope_ceased", material.code,
           {"reason": reason, "was_open": was_open, "stopped_grants": stopped})


def _recompute_material(material):
    scopes = list(material.scope_rows.all())
    if any(s.state == "open" for s in scopes):
        material.status = m.MAT_OPEN
    else:
        orders = list(material.takedown_orders.all())
        if orders:
            if all(o.status == m.TD_RECEIPTED for o in orders):
                material.status = m.MAT_TAKEN_DOWN
            else:
                material.status = m.MAT_TAKE_DOWN_PENDING
        elif any(s.state in ("ready", "submitted", "blocked", "superseded")
                 for s in scopes):
            material.status = m.MAT_BLOCKED if any(
                s.state == "blocked" for s in scopes) else m.MAT_READY
        else:
            material.status = m.MAT_REGISTERED
    material.save()


# ---------------------------------------------------------------- 可解释查询

def explain(actor, *, purpose, code=None, date_from=None, date_to=None,
            audience=None, region=None, on_date=None):
    """
    授权人员按片段、日期、用途查询：每项给出可见/遮蔽/需重新申请的原因、
    所依据的同意版本，以及来源谱系与派生材料当前处置状态。
    """
    if not actor or not actor.roles:
        raise DomainError("authorized_personnel_only")
    on_date = on_date or now().date()
    qs = m.Material.objects.filter(kind=m.KIND_CLIP)
    if code:
        qs = qs.filter(code=code)
    if date_from:
        qs = qs.filter(recorded_date__gte=date_from)
    if date_to:
        qs = qs.filter(recorded_date__lte=date_to)
    # 每个标识只解释其当前最新内容版本，按日期与标识稳定排序
    codes = sorted(qs.values_list("code", flat=True).distinct())
    materials, _ = latest_versions(codes)
    ordered = sorted(materials.values(),
                     key=lambda x: (x.recorded_date, x.code))
    rows = []
    for material in ordered:
        scope, reason = _visible_scope(material, purpose, audience, region, on_date)
        rows.append({
            "code": material.code,
            "kind": material.kind,
            "recorded_date": material.recorded_date.isoformat(),
            "content_version": material.content_version,
            "visibility": "visible" if scope else _visibility_label(reason),
            "reason": reason or "visible",
            "consent_chain": scope.consent.chain_id if scope else None,
            "consent_version": scope.consent.version if scope else None,
            "provenance": _provenance(material),
            "derivatives": _derivative_dispositions(material),
        })
    return rows


def _visibility_label(reason):
    if reason in ("revoked_reapply", "expired_reapply"):
        return "reapply"
    return "obscured"


def _provenance(material):
    chain = []
    node = material
    while node.derived_from_id is not None:
        node = node.derived_from
        chain.append({"code": node.code, "content_version": node.content_version,
                      "source_consent": (node.source_consent.version
                                         if node.source_consent else None)})
    return list(reversed(chain))


def _derivative_dispositions(material):
    result = []

    def walk(mat):
        for child in m.Material.objects.filter(derived_from=mat):
            latest = (m.Material.objects.filter(code=child.code)
                      .order_by("-content_version").first())
            open_scope = next(
                (s for s in latest.scope_rows.all() if s.state == "open"), None
            )
            order = latest.takedown_orders.order_by("-created_at").first()
            result.append({
                "code": latest.code,
                "kind": latest.kind,
                "content_version": latest.content_version,
                "material_status": latest.status,
                "scope_state": open_scope.state if open_scope else (
                    "taken_down" if latest.takedown_orders.exists() else "not_open"),
                "takedown": None if order is None else
                {"reason": order.reason, "status": order.status},
            })
            walk(child)

    walk(material)
    return result
