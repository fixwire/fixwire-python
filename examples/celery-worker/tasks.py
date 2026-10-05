"""Invoice tasks for a Celery worker, reported to Fixwire.

celery -A tasks worker --loglevel=info     # the worker
python send.py                             # queue some work
"""

from __future__ import annotations  # celery's Task is generic only in its stubs

import os
import random

from celery import Celery, Task

import fixwire
from fixwire.integrations.celery import CeleryIntegration

fixwire.init(
    dsn=os.environ.get("FIXWIRE_DSN"),
    release=os.environ.get("RELEASE", "billing-worker@1.0.0"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    integrations=[CeleryIntegration()],
    # A trace per task run, continued from the producer's when it sent one.
    traces_sample_rate=float(os.environ.get("TRACES_SAMPLE_RATE", "1.0")),
)

app = Celery("billing", broker=os.environ.get("BROKER_URL", "redis://localhost:6379/0"))


class TemporaryFailure(Exception):
    pass


@app.task(name="billing.generate_invoice", bind=True, max_retries=3, default_retry_delay=5)
def generate_invoice(self: Task[[str, str], str], account_id: str, month: str) -> str:
    # Tags and breadcrumbs belong to this task run only.
    fixwire.set_tag("account", account_id)
    fixwire.add_breadcrumb(category="billing", message="rendering invoice", data={"month": month})
    try:
        if random.random() < float(os.environ.get("FLAKY_RATE", "0")):
            raise TemporaryFailure("PDF renderer busy")
    except TemporaryFailure as e:
        # Retries are not reported; only the final failure is.
        raise self.retry(exc=e) from e
    with fixwire.start_span("render PDF", op="pdf.render"):
        if account_id.startswith("closed-"):
            raise LookupError("account %s has no billing address" % account_id)
        return "invoice-%s-%s.pdf" % (account_id, month)
