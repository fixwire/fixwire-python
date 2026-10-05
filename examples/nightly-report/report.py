"""A nightly job (cron, a Kubernetes CronJob, a CI step) that builds one
report per account and must not stop at the first failure.

It uses a Client directly (no global setup) and the offline queue: if the
network is down at 3 a.m., failures are kept on disk and sent on the next
run. Check-ins tell the "nightly-report" monitor when the job ran and how it
went, so a job that never starts is noticed too.

    FIXWIRE_DSN=https://<key>@<host> python report.py
"""

import os
import sys
import time

import fixwire

ACCOUNTS = ["acme", "globex", "initech", "umbrella"]


def build_report(account: str) -> int:
    rows = {"acme": 120, "globex": 87, "umbrella": 4}[account]  # initech is missing: KeyError
    return rows


def main() -> int:
    failed = 0
    with fixwire.Client(
        os.environ.get("FIXWIRE_DSN"),
        release=os.environ.get("RELEASE", "reports@1.0.0"),
        offline=os.environ.get("FIXWIRE_OFFLINE", "1") == "1",
        default_integrations=False,
    ) as client:
        started = time.monotonic()
        run = client.capture_check_in(
            "nightly-report", "in_progress", monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}}
        )
        for account in ACCOUNTS:
            with fixwire.new_scope() as scope:
                scope.set_tag("account", account)
                try:
                    print("%s: %d rows" % (account, build_report(account)))
                except Exception:
                    failed += 1
                    client.capture_exception()
        if failed:
            client.capture_message("nightly report finished with %d failures" % failed, level="warning")
        client.capture_check_in(
            "nightly-report", "error" if failed else "ok", check_in_id=run, duration=time.monotonic() - started
        )
        # Leaving the block flushes (up to shutdown_timeout); anything not
        # sent stays in the offline queue for the next run.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
