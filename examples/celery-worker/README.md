# Celery worker

```sh
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt        # or, from this repo: uv pip install -e ../.. "celery[redis]"
FIXWIRE_DSN=https://<key>@<host> celery -A tasks worker --loglevel=info
python send.py
```

`generate_invoice` fails for `closed-initech`: Fixwire gets one event with
the task name as the transaction, the account tag, the task context (id,
retries) and the breadcrumbs of that run. Retries are not reported; set
`FLAKY_RATE=0.5` to see retries happen without noise. Worker processes
flush before they exit, and forked workers start clean.
