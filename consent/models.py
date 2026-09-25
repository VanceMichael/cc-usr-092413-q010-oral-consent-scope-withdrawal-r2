from django.db import models

# 角色
ROLE_ARCHIVIST = "archivist"        # 档案员：整理范围、提交同意与节选
ROLE_ETHICS = "ethics"              # 伦理复核人：确认限制
ROLE_PUBLISHER = "publisher"        # 发布人：执行开放
ROLE_FAMILY = "family"              # 讲述人家属：可提出撤回/收窄

# 同意状态
CONSENT_DRAFT = "draft"
CONSENT_SUBMITTED = "submitted"
CONSENT_CONFIRMED = "confirmed"
CONSENT_OPEN = "open"
CONSENT_REVOKED = "revoked"
CONSENT_EXPIRED = "expired"
CONSENT_SUPERSEDED = "superseded"

# 材料状态
MAT_REGISTERED = "registered"        # 已登记，尚未经伦理确认
MAT_READY = "ready"                  # 限制已确认，等待发布
MAT_PENDING_OPEN = "pending_open"    # 已排队的后续发布
MAT_OPEN = "open"                    # 已公开
MAT_BLOCKED = "blocked"              # 撤回/收窄后被阻止发布
MAT_TAKE_DOWN_PENDING = "take_down_pending"  # 下架流程中（已不可见）
MAT_TAKEN_DOWN = "taken_down"        # 已下架

# 材料类型
KIND_RECORDING = "recording"
KIND_CLIP = "clip"                   # 主题片段
KIND_TRANSCRIPT = "transcript"       # 转写
KIND_TRANSLATION = "translation"     # 翻译
KIND_EXCERPT = "excerpt"             # 展陈节选

# 访问申请状态
REQ_PENDING = "pending"
REQ_DECIDED = "decided"
REQ_MANUAL_REVIEW = "manual_review"

# 凭据（访问会话）状态
GRANT_ACTIVE = "active"             # 尚未完成的访问
GRANT_COMPLETED = "completed"       # 已经发生的阅览：保留审计事实
GRANT_STOPPED = "stopped"           # 撤回/收窄时被停止的未完成访问

# 下架单状态（下架 → 通知 → 回执）
TD_PENDING = "pending"
TD_DOWN = "down"
TD_NOTIFIED = "notified"
TD_RECEIPTED = "receipted"

# 作业状态
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"


class Actor(models.Model):
    """授权人员：一个人可持有多个角色，但不能批准自己提交的内容。"""

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    roles = models.JSONField(default=list)

    def has_role(self, role):
        return role in (self.roles or [])


class Consent(models.Model):
    """
    每次同意固定八个维度：讲述人、采集场次、用途、受众、地区、期限、
    署名方式、证据摘要。同一 (讲述人, 场次, 用途) 的后续变化以新版本承载。
    """

    chain_id = models.CharField(max_length=64, db_index=True)
    version = models.PositiveIntegerField()
    narrator_code = models.CharField(max_length=64)
    session_code = models.CharField(max_length=64)
    purpose = models.CharField(max_length=32)
    audience = models.JSONField(default=list)   # 允许受众代码集合
    region = models.JSONField(default=list)     # 允许地区代码集合
    valid_from = models.DateField()
    valid_until = models.DateField()
    attribution = models.CharField(max_length=256)
    evidence_summary = models.CharField(max_length=512)

    status = models.CharField(max_length=20, default=CONSENT_SUBMITTED)
    supersedes = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="superseding"
    )
    # 是否为“范围收窄”新版本：开放时才会令范围外的旧覆盖停止并入下架
    is_narrow = models.BooleanField(default=False)

    submitted_by = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT,
                                     related_name="consents_submitted")
    confirmed_by = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT,
                                     related_name="consents_confirmed")
    opened_by = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT,
                                  related_name="consents_opened")

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True)
    opened_at = models.DateTimeField(null=True)
    ended_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = ("chain_id", "version")
        indexes = [
            models.Index(fields=["narrator_code", "session_code", "purpose"]),
            models.Index(fields=["status", "valid_until"]),
        ]

    @property
    def coord(self):
        return f"{self.narrator_code}/{self.session_code}/{self.purpose}"


class Material(models.Model):
    """
    档案材料（录音 / 主题片段 / 转写 / 翻译 / 展陈节选）。
    同一标识可有多个内容版本；派生材料保存来源父本及其版本、以及派生时
    所依据的同意版本（来源范围快照）。
    """

    code = models.CharField(max_length=128, db_index=True)
    content_version = models.PositiveIntegerField(default=1)
    fingerprint = models.CharField(max_length=128)
    kind = models.CharField(max_length=32)
    narrator_code = models.CharField(max_length=64)
    session_code = models.CharField(max_length=64)
    recorded_date = models.DateField()

    # 版本关系：父本指向“具体的父本内容版本”
    derived_from = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="derivatives"
    )
    # 来源范围：派生时父本所依据的同意版本快照
    source_consent = models.ForeignKey(
        Consent, null=True, on_delete=models.PROTECT, related_name="snapshot_materials"
    )
    # 当前覆盖该材料的同意版本（随开放/收窄迁移）
    covered_by = models.ForeignKey(
        Consent, null=True, on_delete=models.PROTECT, related_name="covered_materials"
    )

    status = models.CharField(max_length=20, default=MAT_REGISTERED)

    # 节选等材料需要“提交—确认—发布”，记录提交人以落实职责分离
    submitted_by = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT,
                                     related_name="materials_submitted")
    confirmed_by = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT,
                                     related_name="materials_confirmed")
    restriction_note = models.CharField(max_length=512, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("code", "content_version")
        indexes = [
            models.Index(fields=["recorded_date"]),
            models.Index(fields=["kind", "status"]),
        ]


class ConsentScope(models.Model):
    """档案员整理的范围：某同意版本覆盖的具体材料及其处置状态。"""

    consent = models.ForeignKey(Consent, on_delete=models.CASCADE, related_name="scopes")
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="scope_rows")
    # submitted → ready → open；收窄后 superseded；撤回/收窄移除 → blocked/taken_down
    state = models.CharField(max_length=16, default="submitted")

    class Meta:
        unique_together = ("consent", "material")


class AccessRequest(models.Model):
    """研究/展陈申请。request_key 幂等；同键不同内容转人工核查。"""

    request_key = models.CharField(max_length=128, unique=True)
    applicant = models.ForeignKey(Actor, on_delete=models.PROTECT, related_name="requests")
    purpose = models.CharField(max_length=32)
    audience = models.CharField(max_length=64)
    region = models.CharField(max_length=64)
    on_date = models.DateField()
    requested_codes = models.JSONField(default=list)
    payload_fingerprint = models.CharField(max_length=128)

    status = models.CharField(max_length=20, default=REQ_PENDING)
    decision = models.JSONField(null=True)
    decided_at = models.DateTimeField(null=True)
    manual_review_reason = models.CharField(max_length=256, blank=True, default="")

    # 决定时解析到的材料指纹与同意版本
    fingerprint_snapshot = models.JSONField(default=dict)
    consent_snapshot = models.JSONField(default=dict)
    # 清单版本：可见集合变化即变化，领取时据此保证清单与凭据同版本
    manifest_version = models.CharField(max_length=128, blank=True, default="")
    reuse_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)


class Grant(models.Model):
    """
    访问凭据/访问会话：清单中的一项对应一条凭据，盖戳同意版本。
    active=未完成访问；completed=已发生阅览（审计保留）；stopped=被撤回停止。
    """

    request = models.ForeignKey(AccessRequest, on_delete=models.CASCADE, related_name="grants")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="grants")
    consent_version = models.ForeignKey(Consent, on_delete=models.PROTECT, related_name="grants")
    manifest_version = models.CharField(max_length=128)
    file_credential = models.CharField(max_length=128, unique=True)
    state = models.CharField(max_length=16, default=GRANT_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
    stopped_at = models.DateTimeField(null=True)


class TakedownOrder(models.Model):
    """公开材料撤回/收窄/到期后进入的下架单（不删除历史）。"""

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="takedown_orders")
    consent_version = models.ForeignKey(Consent, on_delete=models.PROTECT, related_name="takedown_orders")
    reason = models.CharField(max_length=32)  # revoked / narrowed / expired
    status = models.CharField(max_length=16, default=TD_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    down_at = models.DateTimeField(null=True)
    notified_at = models.DateTimeField(null=True)
    receipted_at = models.DateTimeField(null=True)


class Notice(models.Model):
    """下架通知：发给曾领取该材料的人，幂等。"""

    order = models.ForeignKey(TakedownOrder, on_delete=models.CASCADE, related_name="notices")
    recipient = models.ForeignKey(Actor, on_delete=models.PROTECT, related_name="notices")
    state = models.CharField(max_length=16, default="pending")  # pending/sent
    sent_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = ("order", "recipient")


class Receipt(models.Model):
    """通知回执：每条通知对应一条回执。"""

    notice = models.OneToOneField(Notice, on_delete=models.PROTECT, related_name="receipt")
    recorded_at = models.DateTimeField(auto_now_add=True)
    detail = models.CharField(max_length=512, blank=True, default="")


class Job(models.Model):
    """到期/下架作业：游标检查点保证中断后继续原任务。"""

    kind = models.CharField(max_length=32)  # expiry / takedown
    ref_id = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=16, default=JOB_PENDING)
    cursor = models.JSONField(default=dict)
    runs = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AuditEvent(models.Model):
    """审计事实只增不改：撤回不删除已发生阅览与处置记录。"""

    at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(Actor, null=True, on_delete=models.PROTECT, related_name="audit_events")
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True, default="")
    detail = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["action", "target"])]
