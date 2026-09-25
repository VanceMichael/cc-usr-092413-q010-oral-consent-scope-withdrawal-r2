import unittest
from datetime import date

try:
    from tests import bootstrap  # noqa: F401  manage.py test 方式
except ImportError:
    import bootstrap  # type: ignore  # noqa: F401  unittest discover -s tests 方式

from django.core.management import call_command

from consent import models as m
from consent import services as s
from consent import jobs as j


TODAY = date(2026, 9, 25)
LATER = date(2030, 12, 31)


class DbCase(unittest.TestCase):
    """每个用例前清空内存库（普通 unittest 运行器下的事务隔离替代）。"""

    def setUp(self):
        call_command("flush", interactive=False, verbosity=0)


def actor(code, name, *roles):
    return m.Actor.objects.create(code=code, name=name, roles=list(roles))


class WorldMixin:
    """搭建一个‘录音→主题片段→转写/翻译/展陈节选’的标准世界。"""

    def setUp(self):
        super().setUp()
        self.build_world()

    def build_world(self):
        self.arch = actor("arch", "档案员甲", m.ROLE_ARCHIVIST)
        self.arch2 = actor("arch2", "档案员乙", m.ROLE_ARCHIVIST)
        self.ethics = actor("eth", "伦理人甲", m.ROLE_ETHICS)
        self.pub = actor("pub", "发布人甲", m.ROLE_PUBLISHER)
        self.family = actor("fam", "讲述人家属", m.ROLE_FAMILY)
        self.researcher = actor("res", "研究员丙", "researcher")

        # 1) 登记录音
        self.rec = s.register_material(
            actor=self.arch, code="rec-1", kind=m.KIND_RECORDING,
            content_fingerprint="fp-rec", narrator_code="n1", session_code="s1",
            recorded_date=date(2026, 1, 10))

        # 2) 档案员整理研究用途范围并提交同意（八字段一次固定）
        self.research_v1 = s.submit_consent(
            actor=self.arch, narrator_code="n1", session_code="s1",
            purpose="research", audience=["researcher"], region=["internal"],
            valid_from=date(2026, 1, 1), valid_until=LATER,
            attribution="讲述人 N1 口述项目",
            evidence_summary="书面同意书编号 A-2026-001",
            material_codes=["rec-1"])
        self.research_v1 = s.confirm_consent(self.research_v1, self.ethics)
        self.research_v1 = s.open_consent(self.research_v1, self.pub)

        # 3) 录音开放后派生片段/转写/翻译，来源范围快照记录所依据的同意版本
        self.clip = s.register_material(
            actor=self.arch, code="clip-1", kind=m.KIND_CLIP,
            content_fingerprint="fp-clip", narrator_code="n1", session_code="s1",
            recorded_date=date(2026, 1, 10),
            parent=self.rec, source_purpose="research")
        self.transcript = s.register_material(
            actor=self.arch, code="tr-1", kind=m.KIND_TRANSCRIPT,
            content_fingerprint="fp-tr", narrator_code="n1", session_code="s1",
            recorded_date=date(2026, 1, 10), parent=self.clip)
        self.excerpt = s.register_material(
            actor=self.arch, code="exc-1", kind=m.KIND_EXCERPT,
            content_fingerprint="fp-exc", narrator_code="n1", session_code="s1",
            recorded_date=date(2026, 1, 10),
            parent=self.clip, source_purpose="exhibition")
        # 4) 新版本同意把派生材料纳入研究范围
        self.research_v2 = s.submit_consent(
            actor=self.arch, narrator_code="n1", session_code="s1",
            purpose="research", audience=["researcher"], region=["internal"],
            valid_from=date(2026, 1, 1), valid_until=LATER,
            attribution="讲述人 N1 口述项目",
            evidence_summary="书面同意书编号 A-2026-001（范围扩至片段/转写）",
            material_codes=["clip-1", "tr-1"])
        self.research_v2 = s.confirm_consent(self.research_v2, self.ethics)
        self.research_v2 = s.open_consent(self.research_v2, self.pub)

        # 5) 展陈用途是另一条链，只覆盖展陈节选，受众/地区不同
        self.exhibit_v1 = s.submit_consent(
            actor=self.arch, narrator_code="n1", session_code="s1",
            purpose="exhibition", audience=["public"], region=["web"],
            valid_from=date(2026, 1, 1), valid_until=LATER,
            attribution="讲述人 N1（化名）",
            evidence_summary="展陈补充同意书 A-2026-002",
            material_codes=["exc-1"])
        self.exhibit_v1 = s.confirm_consent(self.exhibit_v1, self.ethics)
        self.exhibit_v1 = s.open_consent(self.exhibit_v1, self.pub)


class ConsentModelTests(DbCase):
    def test_eight_fields_fixed_and_versioned(self):
        a = actor("a", "甲", m.ROLE_ARCHIVIST)
        e = actor("e", "乙", m.ROLE_ETHICS)
        p = actor("p", "丙", m.ROLE_PUBLISHER)
        s.register_material(actor=a, code="r", kind=m.KIND_RECORDING,
                           content_fingerprint="f", narrator_code="n",
                           session_code="s", recorded_date=TODAY)
        c1 = s.submit_consent(
            actor=a, narrator_code="n", session_code="s", purpose="research",
            audience=["researcher"], region=["internal"],
            valid_from=TODAY, valid_until=LATER,
            attribution="署名 N", evidence_summary="证据摘要 X",
            material_codes=["r"])
        self.assertEqual((c1.chain_id, c1.version), ("n|s|research", 1))
        self.assertEqual(c1.status, m.CONSENT_SUBMITTED)
        self.assertEqual(c1.evidence_summary, "证据摘要 X")
        self.assertEqual(set(c1.scopes.values_list("material__code", flat=True)),
                         {"r"})
        s.confirm_consent(c1, e)
        s.open_consent(c1, p)

        # 同链再次提交产生新版本并保留取代关系
        c2 = s.submit_consent(
            actor=a, narrator_code="n", session_code="s", purpose="research",
            audience=["researcher"], region=["internal"],
            valid_from=TODAY, valid_until=LATER,
            attribution="署名 N", evidence_summary="证据摘要 Y",
            material_codes=["r"])
        self.assertEqual(c2.version, 2)
        self.assertEqual(c2.supersedes_id, c1.id)


class ProvenanceTests(WorldMixin, DbCase):
    def test_derived_materials_keep_source_scope_and_version_link(self):
        self.assertEqual(self.clip.derived_from_id, self.rec.id)
        self.assertEqual(self.clip.derived_from.content_version, 1)
        self.assertEqual(self.clip.source_consent_id, self.research_v1.id)
        self.assertEqual(self.transcript.derived_from.code, "clip-1")
        # 转写登记时片段尚未有开放覆盖，回溯到片段的来源范围快照
        self.assertEqual(self.transcript.source_consent_id, self.research_v1.id)
        self.assertEqual(self.excerpt.source_consent_id, self.research_v1.id)

        # 同标识再次登记 = 内容新版本，版本关系承继
        clip_v2 = s.register_material(
            actor=self.arch, code="clip-1", kind=m.KIND_CLIP,
            content_fingerprint="fp-clip-v2", narrator_code="n1",
            session_code="s1", recorded_date=date(2026, 1, 10))
        self.assertEqual(clip_v2.content_version, 2)
        self.assertEqual(clip_v2.derived_from_id, self.clip.id)


class SeparationOfDutiesTests(WorldMixin, DbCase):
    def _fresh_consent(self, codes=("rec-1",), session="sx"):
        return s.submit_consent(
            actor=self.arch, narrator_code="nx", session_code=session,
            purpose="research", audience=["researcher"], region=["internal"],
            valid_from=TODAY, valid_until=LATER, attribution="x",
            evidence_summary="x", material_codes=list(codes))

    def test_archivist_cannot_confirm_and_cannot_confirm_own_submission(self):
        c = self._fresh_consent(session="sx-a")
        with self.assertRaises(s.DomainError):
            s.confirm_consent(c, self.arch)   # 提交者本人不能确认
        with self.assertRaises(s.DomainError):
            s.confirm_consent(c, self.pub)    # 发布角色不能做伦理确认

    def test_publisher_cannot_open_and_must_be_distinct(self):
        c = self._fresh_consent(session="sx-b")
        with self.assertRaises(s.DomainError):
            s.open_consent(c, self.arch)      # 档案员不能发布
        c2 = self._fresh_consent(session="sx-c")
        c2 = s.confirm_consent(c2, self.ethics)
        with self.assertRaises(s.DomainError):
            s.open_consent(c2, self.ethics)   # 确认者不能发布
        c2 = s.open_consent(c2, self.pub)
        self.assertEqual(c2.status, m.CONSENT_OPEN)

    def test_nobody_may_approve_own_excerpt(self):
        # 节选由另一名档案员提交：即使同意提交人不同，
        # 伦理/发布人也不能与节选提交者为同一人
        excerpt = s.register_material(
            actor=self.arch2, code="exc-own", kind=m.KIND_EXCERPT,
            content_fingerprint="fp-own", narrator_code="n1",
            session_code="s1", recorded_date=date(2026, 1, 10),
            parent=self.clip)
        c = s.submit_consent(
            actor=self.arch, narrator_code="n1", session_code="s1",
            purpose="exhibition-own", audience=["public"], region=["web"],
            valid_from=TODAY, valid_until=LATER, attribution="N",
            evidence_summary="own", material_codes=["exc-own"])
        # 伦理人恰为节选提交人本人（兼任）→ 禁止确认
        self.arch2.roles = [m.ROLE_ARCHIVIST, m.ROLE_ETHICS]
        self.arch2.save()
        with self.assertRaises(s.DomainError):
            s.confirm_consent(c, self.arch2)


class ManifestAndIdempotencyTests(WorldMixin, DbCase):
    def _research_request(self, key="req-1", codes=("clip-1", "tr-1", "exc-1")):
        return s.submit_request(
            actor=self.researcher, request_key=key, purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=list(codes))

    def test_minimum_visible_list_matches_purpose(self):
        req, reused = self._research_request()
        self.assertFalse(reused)
        manifest = s.visible_manifest(req)
        self.assertEqual([i["code"] for i in manifest],
                         ["clip-1", "tr-1"])
        # 展陈节选在研究用途下不可见，并给出原因
        reasons = {i["code"]: i["reason"] for i in req.decision["items"]}
        self.assertEqual(reasons["exc-1"], "no_open_consent")
        for item in manifest:
            self.assertEqual(set(item), {"code", "content_version",
                                       "consent_version"})

    def test_exhibition_request_only_sees_excerpt(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-ex", purpose="exhibition",
            audience="public", region="web", on_date=TODAY,
            requested_codes=["clip-1", "tr-1", "exc-1"])
        self.assertEqual([i["code"] for i in s.visible_manifest(req)], ["exc-1"])
        reasons = {i["code"]: i["reason"] for i in req.decision["items"]}
        self.assertEqual(reasons["clip-1"], "no_open_consent")

    def test_audience_and_region_and_expiry_obscure_or_reapply(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-aud", purpose="research",
            audience="student", region="internal", on_date=TODAY,
            requested_codes=["clip-1"])
        self.assertEqual(req.decision["items"][0]["reason"],
                         "audience_not_covered")
        req2, _ = s.submit_request(
            actor=self.researcher, request_key="req-reg", purpose="research",
            audience="researcher", region="us", on_date=TODAY,
            requested_codes=["clip-1"])
        self.assertEqual(req2.decision["items"][0]["reason"],
                         "region_not_covered")
        req3, _ = s.submit_request(
            actor=self.researcher, request_key="req-exp", purpose="research",
            audience="researcher", region="internal",
            on_date=date(2031, 1, 1), requested_codes=["clip-1"])
        self.assertEqual(req3.decision["items"][0]["reason"], "expired_reapply")

    def test_same_request_retries_reuse_original_decision(self):
        req1, reused1 = self._research_request()
        self.assertFalse(reused1)
        req2, reused2 = self._research_request()
        self.assertTrue(reused2)
        self.assertEqual(req1.id, req2.id)
        self.assertEqual(req2.reuse_count, 1)
        self.assertEqual(req2.manifest_version, req1.manifest_version)

    def test_same_key_different_content_goes_manual(self):
        self._research_request(key="req-k")
        req, reused = s.submit_request(
            actor=self.researcher, request_key="req-k", purpose="research",
            audience="researcher", region="us", on_date=TODAY,
            requested_codes=["clip-1"])
        self.assertFalse(reused)
        self.assertEqual(req.status, m.REQ_MANUAL_REVIEW)
        self.assertEqual(req.manual_review_reason,
                         "same_key_different_content")

    def test_same_identifier_different_fingerprint_goes_manual(self):
        req, _ = self._research_request(key="req-fp")
        self.assertEqual(req.status, m.REQ_DECIDED)
        # 档案员以同一标识登记了内容不同的新版本
        s.register_material(
            actor=self.arch, code="clip-1", kind=m.KIND_CLIP,
            content_fingerprint="fp-clip-changed", narrator_code="n1",
            session_code="s1", recorded_date=date(2026, 1, 10))
        retried, reused = self._research_request(key="req-fp")
        self.assertFalse(reused)
        self.assertEqual(retried.status, m.REQ_MANUAL_REVIEW)
        self.assertEqual(retried.manual_review_reason,
                         "same_identifier_different_content")


class ClaimConcurrencyTests(WorldMixin, DbCase):
    def _request_and_claim_ok(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-c", purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=["clip-1", "tr-1"])
        creds = s.claim(req, self.researcher)
        return req, creds

    def test_claim_uses_same_consent_version_as_manifest(self):
        req, creds = self._request_and_claim_ok()
        by_code = {c["code"]: c for c in creds}
        self.assertEqual(by_code["clip-1"]["consent_version"],
                         self.research_v2.version)
        # 清单版本与凭据盖戳一致
        self.assertTrue(all(c["state"] == m.GRANT_ACTIVE for c in creds))
        # 重复领取沿用原凭据（幂等）
        again = s.claim(req, self.researcher)
        self.assertEqual({c["credential"] for c in again},
                         {c["credential"] for c in creds})

    def test_claim_concurrent_with_revocation_is_rejected(self):
        req, creds = self._request_and_claim_ok()
        self.assertEqual(len(creds), 2)
        # 撤回恰在领取之后：已发凭据的未完成访问被停止
        s.revoke_consent(self.research_v2, self.family)
        with self.assertRaises(s.DomainError):
            s.claim(req, self.researcher)  # 清单与凭据不能按不同版本决定
        self.assertTrue(
            m.Grant.objects.filter(state=m.GRANT_STOPPED).count() >= 2)

    def test_non_applicant_cannot_claim(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-x", purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=["clip-1"])
        with self.assertRaises(s.DomainError):
            s.claim(req, self.arch)


class RevocationTests(WorldMixin, DbCase):
    def _granted(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-r", purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=["clip-1", "tr-1"])
        creds = s.claim(req, self.researcher)
        return req, {c["code"]: c for c in creds}

    def _run_orders(self):
        for order in m.TakedownOrder.objects.all():
            j.run_job(j.enqueue_takedown(order.pk).pk)

    def test_revocation_stops_access_keeps_audit_enters_takedown_flow(self):
        req, creds = self._granted()
        clip_grant = m.Grant.objects.get(
            file_credential=creds["clip-1"]["credential"])
        # clip-1 的阅览已经完成：撤回后必须保留审计事实
        s.complete_grant(clip_grant, self.researcher)

        s.revoke_consent(self.research_v2, self.family)

        # 撤回覆盖该讲述人场次下全部用途（研究与展陈均停止后续发布）
        self.assertEqual(
            m.Consent.objects.filter(
                narrator_code="n1", session_code="s1")
            .exclude(status__in=[m.CONSENT_REVOKED]).count(),
            0)
        clip_grant.refresh_from_db()
        self.assertEqual(clip_grant.state, m.GRANT_COMPLETED)
        self.assertTrue(m.AuditEvent.objects.filter(
            action="access_completed").exists())
        stopped = set(m.Grant.objects.filter(
            state=m.GRANT_STOPPED).values_list("material__code", flat=True))
        self.assertIn("tr-1", stopped)
        self.assertNotIn("clip-1", stopped)

        # 公开材料没有被删除，而是进入下架流程
        self.assertTrue(m.TakedownOrder.objects.exists())
        self._run_orders()
        self.assertTrue(all(o.status == m.TD_RECEIPTED
                            for o in m.TakedownOrder.objects.all()))
        # 通知发给曾领取的人，且每条通知都有回执
        notices = m.Notice.objects.filter(recipient=self.researcher)
        self.assertGreaterEqual(notices.count(), 1)
        self.assertEqual(notices.filter(receipt__isnull=True).count(), 0)
        self.assertTrue(
            m.Material.objects.filter(code="clip-1",
                                     status=m.MAT_TAKEN_DOWN).exists())

        # 撤回后的申请看到“需重新申请”
        new_req, _ = s.submit_request(
            actor=self.researcher, request_key="req-after",
            purpose="research", audience="researcher", region="internal",
            on_date=TODAY, requested_codes=["clip-1"])
        self.assertEqual(new_req.decision["items"][0]["reason"],
                         "revoked_reapply")

    def test_audit_history_is_append_only(self):
        self._granted()
        before = m.AuditEvent.objects.count()
        s.revoke_consent(self.research_v2, self.family)
        self.assertGreater(m.AuditEvent.objects.count(), before)
        # 历史材料与凭据记录依然存在
        self.assertEqual(m.Material.objects.count(), 4)
        self.assertGreaterEqual(m.Grant.objects.count(), 2)


class NarrowingTests(DbCase):
    def setUp(self):
        self.arch = actor("a2", "档案员", m.ROLE_ARCHIVIST)
        self.ethics = actor("e2", "伦理人", m.ROLE_ETHICS)
        self.pub = actor("p2", "发布人", m.ROLE_PUBLISHER)
        self.family = actor("f2", "家属", m.ROLE_FAMILY)
        self.researcher = actor("r2", "研究者", "researcher")
        s.register_material(actor=self.arch, code="keep", kind=m.KIND_CLIP,
                            content_fingerprint="fp-k", narrator_code="n2",
                            session_code="s2", recorded_date=TODAY)
        s.register_material(actor=self.arch, code="drop", kind=m.KIND_CLIP,
                            content_fingerprint="fp-d", narrator_code="n2",
                            session_code="s2", recorded_date=TODAY)
        self.v1 = s.submit_consent(
            actor=self.arch, narrator_code="n2", session_code="s2",
            purpose="research", audience=["researcher"], region=["internal"],
            valid_from=TODAY, valid_until=LATER, attribution="N2",
            evidence_summary="同意书 B-1", material_codes=["keep", "drop"])
        s.confirm_consent(self.v1, self.ethics)
        s.open_consent(self.v1, self.pub)
        self.req, _ = s.submit_request(
            actor=self.researcher, request_key="req-n", purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=["keep", "drop"])
        self.creds = s.claim(self.req, self.researcher)
        self.keep_cred = next(c for c in self.creds if c["code"] == "keep")

    def test_narrowing_takes_down_removed_and_migrates_kept(self):
        v2 = s.narrow_consent(self.v1, self.arch, kept_codes=["keep"])
        s.confirm_consent(v2, self.ethics)
        s.open_consent(v2, self.pub)   # 开放新范围的同一动作内完成收窄

        # 移除出范围的片段：停止访问并入下架流程
        dropped_grant = m.Grant.objects.get(
            material__code="drop", state=m.GRANT_STOPPED)
        self.assertTrue(
            m.TakedownOrder.objects.filter(material__code="drop").exists())
        # 保留片段的未完成访问迁移到新同意版本
        kept_grant = m.Grant.objects.get(
            file_credential=self.keep_cred["credential"])
        self.assertEqual(kept_grant.state, m.GRANT_ACTIVE)
        self.assertEqual(kept_grant.consent_version_id, v2.id)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, m.CONSENT_SUPERSEDED)

        # 旧决定按旧版本，再次领取因清单版本变化而被拒，须重新申请
        with self.assertRaises(s.DomainError):
            s.claim(self.req, self.researcher)


class JobResumptionTests(WorldMixin, DbCase):
    def test_takedown_job_resumes_after_interruption(self):
        req, _ = s.submit_request(
            actor=self.researcher, request_key="req-j", purpose="research",
            audience="researcher", region="internal", on_date=TODAY,
            requested_codes=["clip-1", "tr-1"])
        s.claim(req, self.researcher)
        s.revoke_consent(self.research_v2, self.family)
        order = m.TakedownOrder.objects.filter(material__code="clip-1").first()
        job = j.enqueue_takedown(order.pk)
        job.cursor["break_after"] = "notify"
        job.save()

        with self.assertRaises(j.JobInterrupted):
            j.run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, m.JOB_PENDING)
        self.assertTrue(job.cursor["down_done"])
        self.assertGreaterEqual(job.cursor["notify_idx"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, m.TD_DOWN)

        # 中断后继续原任务（同一个 job），不产生重复通知/回执
        job.cursor.pop("break_after")
        job.save()
        j.run_job(job.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, m.TD_RECEIPTED)
        recipients = m.Notice.objects.filter(order=order)
        self.assertEqual(recipients.count(),
                         recipients.values_list("recipient", flat=True).distinct().count())
        self.assertEqual(m.Receipt.objects.filter(notice__order=order).count(),
                         recipients.count())

    def test_expiry_job_resumes_after_interruption(self):
        ids = j.due_expiry_consents(date(2031, 1, 1))
        self.assertIn(self.research_v2.id, ids)
        job = j.enqueue_expiry(ids, date(2031, 1, 1))
        job.cursor["break_after"] = "expire"
        job.save()
        with self.assertRaises(j.JobInterrupted):
            j.run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.cursor["idx"], 1)
        self.assertEqual(job.status, m.JOB_PENDING)
        job.cursor.pop("break_after")
        job.save()
        job = j.run_job(job.pk)  # 继续原任务
        self.assertEqual(job.status, m.JOB_DONE)
        self.research_v2.refresh_from_db()
        self.assertEqual(self.research_v2.status, m.CONSENT_EXPIRED)
        self.assertTrue(
            m.TakedownOrder.objects.filter(
                material__code="clip-1", reason="expired").exists())


class ExplainTests(WorldMixin, DbCase):
    def test_explain_shows_why_visible_obscured_reapply_and_derivatives(self):
        rows = s.explain(
            self.researcher, purpose="research", audience="researcher",
            region="internal", on_date=TODAY)
        by_code = {r["code"]: r for r in rows}
        clip = by_code["clip-1"]
        self.assertEqual(clip["visibility"], "visible")
        self.assertEqual(clip["consent_version"], self.research_v2.version)
        # 来源谱系：片段派生自录音；录音是源头，自身无来源同意，
        # 而片段登记时保存了来源范围快照
        self.assertEqual(clip["provenance"][0]["code"], "rec-1")
        self.assertIsNone(clip["provenance"][0]["source_consent"])
        self.clip.refresh_from_db()
        self.assertEqual(self.clip.source_consent_id, self.research_v1.id)
        kids = {d["code"]: d for d in clip["derivatives"]}
        self.assertIn("tr-1", kids)
        self.assertEqual(kids["tr-1"]["material_status"], m.MAT_OPEN)

        # 撤回后解释变为 reapply，并体现派生材料的处置状态
        s.revoke_consent(self.research_v2, self.family)
        for order in m.TakedownOrder.objects.all():
            j.run_job(j.enqueue_takedown(order.pk).pk)
        rows = s.explain(
            self.researcher, purpose="research", audience="researcher",
            region="internal", on_date=TODAY)
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(by_code["clip-1"]["visibility"], "reapply")
        self.assertEqual(by_code["clip-1"]["reason"], "revoked_reapply")
        kids = {d["code"]: d for d in by_code["clip-1"]["derivatives"]}
        self.assertEqual(kids["tr-1"]["scope_state"], "taken_down")

    def test_explain_rejects_unauthorized(self):
        anon = actor("anon", "无角色人员")
        with self.assertRaises(s.DomainError):
            s.explain(anon, purpose="research")

    def test_explain_filter_by_date(self):
        rows = s.explain(
            self.researcher, purpose="research", audience="researcher",
            region="internal", on_date=TODAY,
            date_from=date(2026, 2, 1))
        self.assertEqual(rows, [])
