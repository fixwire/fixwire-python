# Nightly report (a cron job)

```sh
uv venv && source .venv/bin/activate
uv pip install fixwire        # or, from this repo: uv pip install -e ../..
FIXWIRE_DSN=https://<key>@<host> python report.py
```

One account fails; the job carries on, reports the failure with the account
as a tag, then a summary warning, and exits 1. Check-ins tell the
`nightly-report` monitor that the run started and that it ended with an
error (the first check-in creates the monitor, scheduled at 3 a.m.). With
the offline queue on (the default here), what is captured while the network
is down is sent the next time the job runs.
