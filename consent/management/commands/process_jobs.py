"""
作业处理命令：到期巡检入队，并续跑所有未完成作业。

用法：
  python manage.py process_jobs                 # 入队当日到期任务并处理全部待办
  python manage.py process_jobs --no-sweep      # 只处理已有待办，不做到期巡检
  python manage.py process_jobs --job-id ID     # 只处理指定作业
"""
from datetime import date

from django.core.management.base import BaseCommand

from consent import models as m
from consent import jobs as j


class Command(BaseCommand):
    help = "入队到期处置并续跑未完成的到期/下架作业"

    def add_arguments(self, parser):
        parser.add_argument("--no-sweep", action="store_true")
        parser.add_argument("--job-id", type=int, default=None)
        parser.add_argument("--today", default=None)

    def handle(self, *args, **options):
        today = date.fromisoformat(options["today"]) if options["today"] else date.today()

        if options["job_id"] is not None:
            targets = [m.Job.objects.get(pk=options["job_id"])]
        else:
            if not options["no_sweep"]:
                due_ids = j.due_expiry_consents(today)
                if due_ids and not m.Job.objects.filter(
                    kind="expiry", status__in=(m.JOB_PENDING, m.JOB_RUNNING)
                ).exists():
                    job = j.enqueue_expiry(due_ids, today)
                    self.stdout.write(f"enqueued expiry job {job.pk} "
                                      f"with {len(due_ids)} consents")
            targets = list(
                m.Job.objects.filter(status__in=(m.JOB_PENDING, m.JOB_RUNNING))
                .order_by("pk")
            )

        for job in targets:
            try:
                j.run_job(job.pk)
                self.stdout.write(f"job {job.pk} ({job.kind}) -> done")
            except j.JobInterrupted:
                self.stdout.write(f"job {job.pk} ({job.kind}) interrupted; "
                                  f"checkpoint={job.cursor}")
