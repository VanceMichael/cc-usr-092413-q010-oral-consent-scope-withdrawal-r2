"""
到期与下架作业：步骤幂等 + 游标检查点，中断后继续原任务而非重开新任务。

下架作业步骤固定为：下架(down) → 通知(notify) → 回执(receipt)。
每个最小动作独立提交并推进游标；进程中断时作业回到 pending，
再次运行从游标位置继续，通知与回执靠唯一约束去重。
"""
from __future__ import annotations

from django.db import transaction

from . import models as m
from .services import now
from .services import _audit, _recompute_material


class JobInterrupted(RuntimeError):
    """测试用：在检查点之后模拟进程中断。"""


def enqueue_takedown(order_id: int) -> m.Job:
    job, _ = m.Job.objects.get_or_create(
        kind="takedown", ref_id=order_id,
        defaults={"status": m.JOB_PENDING, "cursor": {}},
    )
    return job


def enqueue_expiry(consent_ids, today) -> m.Job:
    """到期巡检：把当日到期的开放同意打包为一个可续跑作业。"""
    return m.Job.objects.create(
        kind="expiry",
        cursor={"consent_ids": list(consent_ids), "idx": 0, "today": today.isoformat()},
    )


def due_expiry_consents(today):
    return list(
        m.Consent.objects.filter(
            status=m.CONSENT_OPEN, valid_until__lt=today
        ).order_by("pk").values_list("pk", flat=True)
    )


def run_job(job_id: int) -> m.Job:
    job = m.Job.objects.get(pk=job_id)
    if job.status == m.JOB_DONE:
        return job
    with transaction.atomic():
        job.status = m.JOB_RUNNING
        job.runs += 1
        job.save(update_fields=["status", "runs", "updated_at"])
    try:
        if job.kind == "takedown":
            _run_takedown(job)
        elif job.kind == "expiry":
            _run_expiry(job)
        else:
            raise ValueError(f"unknown_job_kind:{job.kind}")
    except JobInterrupted:
        m.Job.objects.filter(pk=job.pk).update(status=m.JOB_PENDING,
                                               last_error="interrupted")
        raise
    except Exception as exc:  # 中断/报错后作业仍可续跑
        m.Job.objects.filter(pk=job.pk).update(status=m.JOB_PENDING,
                                               last_error=str(exc)[:500])
        raise
    return m.Job.objects.get(pk=job.pk)


def _checkpoint(job, **patch):
    job.cursor.update(patch)
    job.save(update_fields=["cursor", "updated_at"])


def _break_after(job, step):
    """游标中携带 break_after 时，在该步骤检查点落库后模拟崩溃。"""
    if job.cursor.get("break_after") == step:
        raise JobInterrupted(step)


# ---------------------------------------------------------------- 下架

def _run_takedown(job: m.Job):
    order = m.TakedownOrder.objects.select_related("material").get(pk=job.ref_id)
    cur = job.cursor

    # 1) 下架：材料即刻不可见，但历史保留
    if not cur.get("down_done"):
        with transaction.atomic():
            order = m.TakedownOrder.objects.select_for_update().get(pk=order.pk)
            if order.status == m.TD_PENDING:
                order.status = m.TD_DOWN
                order.down_at = now()
                order.save(update_fields=["status", "down_at"])
                _recompute_material(order.material)
                _audit(None, "material_taken_down", order.material.code,
                       {"order": order.pk, "reason": order.reason})
        _checkpoint(job, down_done=True)
        _break_after(job, "down")

    # 2) 通知：通知所有曾领取凭据的人（含阅览已完成者），逐条幂等
    recipient_ids = list(
        m.Grant.objects.filter(material=order.material)
        .values_list("request__applicant_id", flat=True)
        .distinct().order_by("request__applicant_id")
    )
    idx = cur.get("notify_idx", 0)
    while idx < len(recipient_ids):
        actor_id = recipient_ids[idx]
        with transaction.atomic():
            notice, created = m.Notice.objects.get_or_create(
                order=order, recipient_id=actor_id,
                defaults={"state": "sent", "sent_at": now()},
            )
            if not created and notice.state != "sent":
                notice.state = "sent"
                notice.sent_at = now()
                notice.save(update_fields=["state", "sent_at"])
        idx += 1
        _checkpoint(job, notify_idx=idx)
        _break_after(job, "notify")
    if order.status != m.TD_NOTIFIED and not m.Receipt.objects.filter(
            notice__order=order).exists():
        order.status = m.TD_NOTIFIED
        order.notified_at = now()
        order.save(update_fields=["status", "notified_at"])

    # 3) 回执：为每条已发通知登记回执
    notice_ids = list(
        m.Notice.objects.filter(order=order).order_by("pk")
        .values_list("pk", flat=True)
    )
    ridx = cur.get("receipt_idx", 0)
    while ridx < len(notice_ids):
        notice_id = notice_ids[ridx]
        with transaction.atomic():
            m.Receipt.objects.get_or_create(
                notice_id=notice_id,
                defaults={"detail": f"order-{order.pk}"},
            )
        ridx += 1
        _checkpoint(job, receipt_idx=ridx)
        _break_after(job, "receipt")

    with transaction.atomic():
        order = m.TakedownOrder.objects.select_for_update().get(pk=order.pk)
        if order.status != m.TD_RECEIPTED:
            order.status = m.TD_RECEIPTED
            order.receipted_at = now()
            order.save(update_fields=["status", "receipted_at"])
            _recompute_material(order.material)
            _audit(None, "takedown_receipted", order.material.code,
                    {"order": order.pk})
    job.status = m.JOB_DONE
    job.save(update_fields=["status", "updated_at"])


# ---------------------------------------------------------------- 到期

def _run_expiry(job: m.Job):
    from .services import expire_consent

    ids = job.cursor.get("consent_ids", [])
    idx = job.cursor.get("idx", 0)
    while idx < len(ids):
        consent = m.Consent.objects.filter(pk=ids[idx]).first()
        if consent is not None:
            expire_consent(consent)
        idx += 1
        _checkpoint(job, idx=idx)
        _break_after(job, "expire")
    job.status = m.JOB_DONE
    job.save(update_fields=["status", "updated_at"])
