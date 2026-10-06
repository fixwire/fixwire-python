# fixwire

[![CI](https://github.com/fixwire/fixwire-python/actions/workflows/ci.yml/badge.svg)](https://github.com/fixwire/fixwire-python/actions/workflows/ci.yml)

The Fixwire SDK for Python: errors today, then traces and AI agents. A sync
client and a native asyncio client share one sans-IO core.

```sh
uv add fixwire              # sync client (urllib3); with pip: pip install fixwire
uv add "fixwire[async]"     # adds the asyncio client (httpx)
```

```python
import fixwire

fixwire.init(dsn="https://fw_pk_live_…@ingest.eu.fixwire.io", release="web@1.4.0")

try:
    charge(order)
except Exception:
    fixwire.capture_exception()
```

The DSN is your project's publishable key and the ingest host,
`https://<key>@<host>`; without the `dsn` option the SDK reads
`FIXWIRE_DSN`, and without either it does nothing.

Inside an event loop, `init()` picks the asyncio client by itself. You can
also choose explicitly:

```python
with fixwire.Client(dsn=...) as client:            # scripts, workers
    client.capture_message("nightly job started")

async with fixwire.AsyncClient(dsn=...) as client:  # asyncio apps
    client.capture_exception(error)
    await client.aflush()
```

**What's different**
- Secrets and personal data are masked on the device, with the same rules
  as the Fixwire server.
- A crash loop costs a few events and a count, not your quota.
- Captures never block your thread or event loop. Delivery retries with
  backoff and honours rate limits, pausing only the kind of data a limit
  names.
- One background thread at most, safe across `fork()`.
- It speaks the Fixwire protocol: errors, messages and spans travel as
  OpenTelemetry's OTLP/HTTP (JSON), with structured stack traces,
  breadcrumbs and redaction on top.

**Feedback:** rate an AI answer, or say what went wrong with a crash. A
negative score opens a `user_feedback` issue for the agent:

```python
fixwire.capture_feedback("Refunded the wrong order", score=-1, trace_id=run_trace_id)
fixwire.capture_feedback("It crashed on save", event_id=fixwire.last_event_id())
```

**Cron jobs:** check in when a scheduled job starts and ends; its monitor
notices runs that fail, take too long or never happen:

```python
run = fixwire.capture_check_in("nightly-report", "in_progress",
                               monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}})
...
fixwire.capture_check_in("nightly-report", "ok", check_in_id=run, duration=42.5)
```

**Frameworks**

```python
from fixwire.integrations.asgi import FixwireMiddleware      # FastAPI, Starlette, any ASGI
app.add_middleware(FixwireMiddleware)

MIDDLEWARE = ["fixwire.integrations.django.FixwireMiddleware", ...]   # Django

from fixwire.integrations.flask import init_app                # Flask
init_app(app)

from fixwire.integrations.wsgi import FixwireMiddleware      # any WSGI app
app = FixwireMiddleware(app)

from fixwire.integrations.celery import CeleryIntegration    # Celery
fixwire.init(dsn=..., integrations=[CeleryIntegration()])
```

Outgoing calls through `httpx` or `requests` (`HttpxIntegration()`,
`RequestsIntegration()`) become child spans with an `http` breadcrumb, and
carry the trace (W3C `traceparent` and `tracestate`, and the incoming
`baggage`) to `trace_propagation_targets` only. Incoming requests and
Celery tasks continue their caller's trace and keep its sampling decision,
so a trace mixing Fixwire and OpenTelemetry services stays whole.

Targets are matched against the URL without its user info, query and
fragment:

```python
fixwire.init(dsn=..., trace_propagation_targets=[
    "internal.example",                # this host and its subdomains (not badinternal.example)
    "billing.example:8443",            # a host on one port
    "https://api.partner.example/v2",  # URLs starting with this
    re.compile(r"^https://[a-z]+\.svc\.cluster\.local/"),  # searched for in the URL
])
```

Each request or task gets its own scope. Request headers come from an
allowlist: cookies and authorization never leave.

**Typed:** the package ships `py.typed` and passes mypy and pyright in
strict mode. Every `init()` option completes in the editor (a misspelled one
is a type error), and callbacks can use the TypedDicts in `fixwire.types`:

```python
from fixwire.types import Event, Hint

def before_send(event: Event, hint: Hint) -> Event | None:
    exc_info = hint.get("exc_info")
    return None if exc_info and isinstance(exc_info[1], ConnectionResetError) else event
```

Callbacks typed with plain dicts are accepted too.

**Offline:** `fixwire.init(dsn=..., offline=True)` keeps what it sends on
disk (SQLite, bounded) until the server has it, across outages and
restarts.

**Release health:** with `release` set, each request (ASGI, WSGI, Django,
Flask) and each `@fixwire.serverless_function` call is a session: exited,
errored or crashed, counted per minute and sent about every minute, so each
release gets crash-free rates. Users leave only as hashes made on your
machine; `auto_session_tracking=False` turns it off.

**Switching SDKs?** The API names are the familiar ones (`init`,
`capture_exception`, `set_tag`, `new_scope`, …). Set `FIXWIRE_DSN`,
`FIXWIRE_RELEASE` and `FIXWIRE_ENVIRONMENT`, or pass them to `init()`.

**Already on OpenTelemetry?** Keep it: `OpenTelemetryIntegration()` puts
your OTel spans' traces on Fixwire's errors, and
`otlp_exporter_options(dsn)` points your own OTLP exporters at Fixwire.
