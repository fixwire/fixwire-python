<a href="https://fixwire.io">
  <img src="https://raw.githubusercontent.com/fixwire/fixwire-python/main/.github/banner.jpg" alt="Fixwire for Python">
</a>
<div align="center">

_Bugs reach production. Fixwire finds them first: errors, traces, logs and
AI agent runs in one place, an AI debugger on every plan, and your data
kept in Europe._

[![Discord](https://img.shields.io/badge/Discord-join%20us-5865F2?logo=discord&logoColor=white)](https://fixwire.io/discord)
[![Slack](https://img.shields.io/badge/Slack-community-4A154B?logo=slack&logoColor=white)](https://fixwire.io/slack)
[![X](https://img.shields.io/badge/X-follow%20us-000000?logo=x&logoColor=white)](https://fixwire.io/x)
[![Release](https://img.shields.io/github/v/release/fixwire/fixwire-python?label=release)](https://github.com/fixwire/fixwire-python/releases)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue?logo=python&logoColor=white)](https://github.com/fixwire/fixwire-python/actions/workflows/ci.yml)
[![CI](https://github.com/fixwire/fixwire-python/actions/workflows/ci.yml/badge.svg)](https://github.com/fixwire/fixwire-python/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/fixwire/fixwire-python/blob/main/LICENSE)

<br/>

</div>

# Fixwire SDK for Python

Welcome to the official Python SDK for **[Fixwire](https://fixwire.io)**.
It captures errors and crashes, logged errors, traces, release health, cron
monitor check-ins, user feedback and AI agent runs, from scripts, web apps,
workers and serverless functions, with a sync client and a native asyncio
client sharing one core.

## 📦 Getting started

### Prerequisites

- A Fixwire account and project: sign up at [fixwire.io](https://fixwire.io).
- Python 3.10 or newer. CI tests 3.10, 3.11, 3.12, 3.13 and 3.14 on Linux,
  and 3.14 on macOS and Windows.

### Installation

With [uv](https://docs.astral.sh/uv/):

```sh
uv add fixwire              # the sync client (urllib3)
uv add "fixwire[async]"     # adds the asyncio client (httpx)
```

With pip:

```sh
pip install fixwire
pip install "fixwire[async]"
```

### Basic configuration

Call `init()` once, as early as you can in your app's startup:

```python
import fixwire

fixwire.init(
    dsn="https://<publishable key>@ingest.eu.fixwire.io",
    release="web@1.4.0",
    environment="production",
    traces_sample_rate=0.2,  # record 20% of traces
    # send_default_pii=True,  # also send users' IP addresses from proxy headers
    # redact=False,           # turn off masking of secrets and personal data
)
```

The DSN is your project's publishable key and the ingest host,
`https://<publishable key>@<host>`. Without the `dsn` option the SDK reads
`FIXWIRE_DSN` (and `FIXWIRE_RELEASE` and `FIXWIRE_ENVIRONMENT`); without
either, it does nothing. `init()` never raises: a broken DSN or option is
said in a warning on stderr and the SDK stays off, so a typo in
configuration can't stop your app from starting.

### Quick usage example

```python
import fixwire

fixwire.capture_message("Hello Fixwire!")  # an info-level message in your project

try:
    1 / 0
except ZeroDivisionError:
    fixwire.capture_exception()  # the error, with its stack, local variables and breadcrumbs
```

Uncaught exceptions, in the main thread and in other threads, are reported
by themselves, and what is queued is sent when the process exits.

## ✨ Why Fixwire

- **Secrets stay on your machine.** Secrets and personal data are masked on
  the device, with the same rules as the Fixwire server, before anything is
  sent. Your app's own configuration (release, environment, service and
  server names) is sent as given.
- **A crash loop costs a few events, not your quota.** Each issue sends a
  burst of 10 events, then 1 a minute, and a count of the rest.
- **It never gets in your app's way.** `init()` and captures never raise
  into your code, and captures never block your thread or event loop.
  Queues, strings, stacks and retries all have fixed limits, and there is
  one background thread at most, safe across `fork()`.
- **OpenTelemetry inside.** It speaks the Fixwire protocol (OpenTelemetry's
  OTLP/HTTP plus a few small JSON endpoints), so its traces join the ones
  your OpenTelemetry services send.
- **Trace headers only where you allow.** Outgoing requests carry trace
  headers only to the hosts and URLs you list.
- **Sync and asyncio, fully typed.** urllib3 is the only dependency (httpx
  for the asyncio client), and the package passes mypy and pyright in
  strict mode.
- **Your data stays in Europe.** Fixwire runs in Europe.

## 🧩 Integrations

| Integration | What it does | How to use |
|---|---|---|
| Uncaught exceptions | Reports exceptions nothing caught, in the main thread and in threads, as unhandled | On by default |
| Exit flush | Sends what is queued when the process exits (up to `shutdown_timeout`) | On by default |
| `logging` | Log records at INFO and up become breadcrumbs, at ERROR and up events, through a handler on the root logger | On by default |
| Django | A scope and a trace per request; view errors with the request and the URL pattern; sessions | `"fixwire.integrations.django.FixwireMiddleware"` in `MIDDLEWARE` |
| Flask | A scope and a trace per request; view errors Flask turns into 500s; sessions | `init_app(app)` from `fixwire.integrations.flask` |
| FastAPI, Starlette and any ASGI app | A scope and a trace per request, errors with the request and the route; the body is never read | `app.add_middleware(FixwireMiddleware)` from `fixwire.integrations.asgi` |
| Any WSGI app | A scope and a trace per request (streamed responses too), errors with the request | `app = FixwireMiddleware(app)` from `fixwire.integrations.wsgi` |
| Celery | A scope and a trace per task, the caller's trace continued; failures reported, retries not | `integrations=[CeleryIntegration()]` |
| asyncio | Delivery from your event loop, never a thread blocking it | `AsyncClient`, or `init()` inside a running loop ([details](https://github.com/fixwire/fixwire-python#sync-and-asyncio-clients)) |
| httpx | Outgoing requests become child spans and carry trace headers to your targets | `integrations=[HttpxIntegration()]` |
| requests | The same for requests, with an `http` breadcrumb | `integrations=[RequestsIntegration()]` |
| OpenAI | Chat completions, responses and embeddings (streamed too) become spans with the model, tokens and finish reasons | `fixwire.ai.wrap_openai(OpenAI())` |
| Anthropic | Messages (streamed too) become chat spans with the model, tokens (cache tokens too) and stop reason | `fixwire.ai.wrap_anthropic(Anthropic())` |
| AI agents | Agent runs, model calls and tool calls with the OpenTelemetry GenAI conventions, for any model | `fixwire.ai.agent()`, `chat()`, `tool()`, `embeddings()` ([details](https://github.com/fixwire/fixwire-python#ai-agents)) |
| OpenTelemetry | Your OpenTelemetry spans' traces on Fixwire's errors; your OTLP exporters pointed at Fixwire | `integrations=[OpenTelemetryIntegration()]`, `otlp_exporter_options(dsn)` |
| Serverless | AWS Lambda, Google Cloud Functions, Azure Functions: a scope and a trace per call, flushed before it returns | `@fixwire.serverless_function` |

### Web frameworks and workers

```python
from fixwire.integrations.asgi import FixwireMiddleware     # FastAPI, Starlette, any ASGI
app.add_middleware(FixwireMiddleware)

MIDDLEWARE = ["fixwire.integrations.django.FixwireMiddleware", ...]  # Django

from fixwire.integrations.flask import init_app             # Flask
init_app(app)

from fixwire.integrations.wsgi import FixwireMiddleware     # any WSGI app
app = FixwireMiddleware(app)

from fixwire.integrations.celery import CeleryIntegration   # Celery
fixwire.init(dsn=..., integrations=[CeleryIntegration()])
```

Each request or task gets its own scope. Request headers come from an
allowlist: cookies and authorization never leave. Incoming requests and
Celery tasks continue their caller's trace and keep its sampling decision,
so a trace mixing Fixwire and OpenTelemetry services stays whole.

### Outgoing HTTP calls

```python
from fixwire.integrations.httpx import HttpxIntegration
from fixwire.integrations.requests import RequestsIntegration

fixwire.init(
    dsn=...,
    traces_sample_rate=0.2,
    integrations=[HttpxIntegration(), RequestsIntegration()],
    trace_propagation_targets=["api.internal.example"],
)
```

Calls through httpx (sync and async) or requests inside a span become child
spans with an `http` breadcrumb. They carry the trace (W3C `traceparent`
and `tracestate`, and the incoming `baggage`) to
`trace_propagation_targets` only.

### AI agents

```python
from anthropic import Anthropic

anthropic = fixwire.ai.wrap_anthropic(Anthropic())  # or fixwire.ai.wrap_openai(OpenAI())

with fixwire.ai.agent("support-bot", provider="anthropic", model="claude-opus-5-5", input=question) as run:
    msg = anthropic.messages.create(  # a chat span: model, tokens, stop reason
        model="claude-opus-5-5", max_tokens=1024, tools=tools,
        messages=[{"role": "user", "content": question}],
    )
    for block in msg.content:
        if block.type == "tool_use":  # a tool span each, failures included
            with fixwire.ai.tool(block.name, call_id=block.id, arguments=block.input) as call:
                call.set_result(run_tool(block.name, block.input))
    run.set_output(msg.content)
```

Prompts, outputs and tool arguments and results are recorded only with
`record_ai_content=True` (or `record_content=True` per call), redacted and
kept to 16 kB each. Without it, tool arguments still get a hash, so an
agent calling the same tool with the same arguments in a loop shows up. For
a model without a wrapper, `fixwire.ai.chat(provider, model)` records a
call by hand.

### OpenTelemetry

Keep your OpenTelemetry setup: Fixwire never sets a tracer provider,
propagator or context.

```python
from fixwire.integrations.opentelemetry import OpenTelemetryIntegration, otlp_exporter_options
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

dsn = os.environ["FIXWIRE_DSN"]
fixwire.init(dsn, integrations=[OpenTelemetryIntegration()])  # your OTel traces on Fixwire's errors
exporter = OTLPSpanExporter(**otlp_exporter_options(dsn)["traces"])  # your spans sent to Fixwire
```

The integration needs `opentelemetry-api`; without it, it does nothing.

### Serverless functions

```python
import os

import fixwire

fixwire.init(os.environ["FIXWIRE_DSN"])

@fixwire.serverless_function
def handler(event, context): ...
```

Async handlers work the same way. An exception the handler raises is
reported and raised on. The flush waits at most `flush_timeout` seconds
(default 2), and on AWS Lambda stays half a second inside the time the
invocation has left.

## ⚙️ Configuration

Pass options to `init()`, `Client()` or `AsyncClient()`. Every option
completes in your editor, and a misspelled one is a type error.

| Option | Default | What it does |
|---|---|---|
| `dsn` | `FIXWIRE_DSN` | Where data goes; none: the SDK does nothing |
| `release` | `FIXWIRE_RELEASE` | Your version, e.g. `"api@1.4.0"`; needed for release health. The part before `@` names the service, unless `OTEL_SERVICE_NAME` is set |
| `environment` | `FIXWIRE_ENVIRONMENT`, then `"production"` | e.g. `"staging"` |
| `dist` | `None` | A build or variant of a release |
| `server_name` | the host name | The machine's name |
| `sample_rate` | `1.0` | Share of errors and messages sent, after the crash-loop budget |
| `traces_sample_rate` | `None` | Share of traces recorded, 0.0 to 1.0; `None` turns tracing off |
| `traces_sampler` | `None` | Decides per trace (see [Sampling](https://github.com/fixwire/fixwire-python#sampling)) |
| `trace_propagation_targets` | `[]` | Where outgoing requests carry trace headers; empty: nowhere |
| `max_breadcrumbs` | `100` | Breadcrumbs kept per scope, the newest |
| `before_send` | `None` | Changes or drops an event before it is sent |
| `before_breadcrumb` | `None` | Changes or drops a breadcrumb |
| `include_local_variables` | `True` | Local variables of your frames, bounded and redacted |
| `include_source_context` | `True` | 5 source lines above and below each frame's line |
| `max_value_length` | `1024` | Longest string sent, in bytes of UTF-8, `...` included (AI content keeps 16 kB) |
| `max_stack_frames` | `100` | Frames kept per exception, the newest |
| `in_app_include`, `in_app_exclude` | `[]` | Modules (and their submodules) that are, or are not, your code |
| `project_root` | the working directory | Frames under it are your code |
| `redact` | `True` | Mask secrets and personal data on the device |
| `sensitive_keys` | built in | Replaces the key fragments (`password`, `token`, `secret`, …) whose values are masked |
| `send_default_pii` | `False` | Send users' IP addresses from proxy headers |
| `rate_limit` | 10 per issue, then 1 a minute; 600 a minute in all | The crash-loop budget: `{"per_issue_burst", "per_issue_per_minute", "global_per_minute", "enabled"}` |
| `transport` | `"auto"` | `"thread"`, `"asyncio"`, or `"auto"`: asyncio inside a running loop with httpx installed, else a thread |
| `shutdown_timeout` | `2.0` | Seconds `close()` and process exit wait for queued data |
| `http_timeout` | `10.0` | Seconds per HTTP request |
| `max_queue_size` | `100` | Requests waiting to be sent, and as many waiting for a retry; past it new data is dropped |
| `offline` | `False` | Keep requests on disk until sent: `True` (a cache directory) or a file path |
| `default_integrations` | `True` | Uncaught exceptions, the exit flush and `logging` |
| `integrations` | `[]` | More integrations, e.g. `[CeleryIntegration()]` |
| `debug` | `False` | Log why data is dropped, to the `fixwire` logger |
| `record_ai_content` | `False` | Record prompts, outputs and tool arguments and results on AI spans, redacted |
| `auto_session_tracking` | `True` | Count each request as a session for crash-free rates (needs `release`) |

### Sync and asyncio clients

Inside a running event loop (with httpx installed), `init()` picks the
asyncio client by itself. You can also create a client yourself:

```python
with fixwire.Client(dsn=...) as client:            # scripts, workers
    client.capture_message("nightly job started")

async with fixwire.AsyncClient(dsn=...) as client:  # asyncio apps
    client.capture_exception(error)
    await client.aflush()
```

Both capture cheaply from any thread or task. `Client` delivers from one
background thread; `AsyncClient` from a task on its event loop, handing
what is left to a thread if the loop goes away. Delivery retries with
backoff and honours rate limits, pausing only the kind of data a limit
names.

### Trace propagation targets

Targets are matched against the URL without its user info, query and
fragment:

```python
fixwire.init(dsn=..., trace_propagation_targets=[
    "internal.example",                # this host and its subdomains (not badinternal.example)
    "billing.example:8443",            # a host on one port
    "https://api.partner.example/v2",  # URLs starting with this
    "/api/",                           # relative URLs starting with this path
    re.compile(r"^https://[a-z]+\.svc\.cluster\.local/"),  # searched for in the URL
])
```

### Filtering events

`before_send` has the last word on an event: return it, changed or not, or
`None` to drop it. Callbacks can use the TypedDicts in `fixwire.types`;
callbacks typed with plain dicts are accepted too.

```python
from fixwire.types import Event, Hint

def before_send(event: Event, hint: Hint) -> Event | None:
    exc_info = hint.get("exc_info")
    return None if exc_info and isinstance(exc_info[1], ConnectionResetError) else event

fixwire.init(dsn=..., before_send=before_send)
```

### Sampling

`sample_rate` keeps a share of errors and messages; `traces_sample_rate`
keeps a share of traces. A `traces_sampler` decides per trace, from the
segment's name, its attributes and the caller's decision; it returns a
rate, `True` or `False`, or `None` to use `traces_sample_rate`:

```python
def traces_sampler(context):
    if context["name"].startswith("GET /health"):
        return 0.0
    return context["parent_sampled"] if context["parent_sampled"] is not None else 0.2

fixwire.init(dsn=..., traces_sampler=traces_sampler)
```

### Redaction

Every string sent from your app's data goes through redaction first:
messages, attributes, span names, breadcrumbs, feedback, URLs and their
queries, local variables and the keys of maps. The rules are the Fixwire
server's, so what is masked on your machine is what the server would mask.
Redaction runs before strings are cut to `max_value_length`, so a secret
the cut goes through is still masked whole. `redact=False` turns it off;
`sensitive_keys` replaces the key fragments whose values are always
masked.

### Release health

With `release` set, each request (ASGI, WSGI, Django, Flask) and each
`@fixwire.serverless_function` call is a session: exited, errored or
crashed, counted per minute and sent about every minute, so each release
gets crash-free rates. Users leave only as hashes made on your machine.
`auto_session_tracking=False` turns it off.

### Cron monitors

Check in when a scheduled job starts and ends; its monitor notices runs
that fail, take too long or never happen:

```python
run = fixwire.capture_check_in("nightly-report", "in_progress",
                               monitor_config={"schedule": {"type": "crontab", "value": "0 3 * * *"}})
...
fixwire.capture_check_in("nightly-report", "ok", check_in_id=run, duration=42.5)
```

### User feedback

Rate an AI answer, or say what went wrong with a crash. A negative score
opens a `user_feedback` issue for the agent:

```python
fixwire.capture_feedback("Refunded the wrong order", score=-1, trace_id=run_trace_id)
fixwire.capture_feedback("It crashed on save", event_id=fixwire.last_event_id())
```

### Offline delivery

`fixwire.init(dsn=..., offline=True)` keeps what it sends on disk (SQLite,
bounded, readable by your user only) until the server has it, across
outages and restarts.

## 🧪 Examples

Real apps, each with its own README and walkthrough, run by the test suite
against a fake ingest so they keep working:

- [fastapi-shop](https://github.com/fixwire/fixwire-python/tree/main/examples/fastapi-shop): the ASGI middleware, per-request users and tags, handled and unhandled errors, `before_send`, a trace per request with a span of your own.
- [flask-notes](https://github.com/fixwire/fixwire-python/tree/main/examples/flask-notes): `init_app()`, per-request users, a streamed response, logged errors, a trace per request.
- [django-blog](https://github.com/fixwire/fixwire-python/tree/main/examples/django-blog): the Django middleware, URL patterns as transactions, 404s not reported, logged errors.
- [celery-worker](https://github.com/fixwire/fixwire-python/tree/main/examples/celery-worker): one scope and one trace per task; retries are not reported, the final failure is.
- [nightly-report](https://github.com/fixwire/fixwire-python/tree/main/examples/nightly-report): a cron job with a `Client`, check-ins to a monitor, the offline queue.
- [asyncio-consumer](https://github.com/fixwire/fixwire-python/tree/main/examples/asyncio-consumer): `AsyncClient`, one scope per message with workers interleaving on one loop.
- [support-agent](https://github.com/fixwire/fixwire-python/tree/main/examples/support-agent): an agent on Claude with agent runs, model calls with tokens, and tool calls and their failures.

## 📚 Documentation

The full guide lives in this README and the examples.

- [Configuration](https://github.com/fixwire/fixwire-python#%EF%B8%8F-configuration)
- [Examples](https://github.com/fixwire/fixwire-python/tree/main/examples)
- [Changelog](https://github.com/fixwire/fixwire-python/blob/main/CHANGELOG.md)
- [Security policy](https://github.com/fixwire/fixwire-python/blob/main/SECURITY.md)
- [Contributing guide](https://github.com/fixwire/fixwire-python/blob/main/CONTRIBUTING.md)

## 🚧 Coming from another error tracker?

The API follows the shape most error-tracking SDKs share: `init`,
`capture_exception`, `capture_message`, `set_user`, `set_tag`,
`add_breadcrumb`, `new_scope`, `isolation_scope` and `start_span`. Moving
over is mostly a change of package and DSN: set `FIXWIRE_DSN`,
`FIXWIRE_RELEASE` and `FIXWIRE_ENVIRONMENT`, or pass them to `init()`. Two
differences: integrations are explicit (frameworks add a middleware, other
libraries an integration, and nothing in other libraries is patched by
default), and trace headers go nowhere until you list
`trace_propagation_targets`.

## 🙌 Want to contribute?

We'd love your help, whether it's a bug report, a fix or a new
integration. Start with the
[contributing guide](https://github.com/fixwire/fixwire-python/blob/main/CONTRIBUTING.md),
then pick from the
[open issues](https://github.com/fixwire/fixwire-python/issues) or the
[good first issues](https://github.com/fixwire/fixwire-python/issues?q=is%3Aopen+label%3A%22good+first+issue%22).

## 🛟 Need help?

- Questions: ask on [Discord](https://fixwire.io/discord) or
  [Slack](https://fixwire.io/slack).
- Bugs: open a [GitHub issue](https://github.com/fixwire/fixwire-python/issues).
- Found a security issue? Please don't open an issue; follow the
  [security policy](https://github.com/fixwire/fixwire-python/blob/main/SECURITY.md).

## 🔗 Resources

- [Website](https://fixwire.io)
- [Pricing](https://fixwire.io/pricing)
- [Discord](https://fixwire.io/discord)
- [Slack](https://fixwire.io/slack)
- [X](https://fixwire.io/x)
- [Changelog](https://github.com/fixwire/fixwire-python/blob/main/CHANGELOG.md)
- [Examples](https://github.com/fixwire/fixwire-python/tree/main/examples)
- [Security policy](https://github.com/fixwire/fixwire-python/blob/main/SECURITY.md)

## 📃 License

The SDK is open source under the MIT license; see
[LICENSE](https://github.com/fixwire/fixwire-python/blob/main/LICENSE).
Parts are derived from other MIT-licensed SDKs; see
[NOTICE](https://github.com/fixwire/fixwire-python/blob/main/NOTICE).

## 😘 Contributors

Thanks to everyone who helps make Fixwire better!

<a href="https://github.com/fixwire/fixwire-python/graphs/contributors"><img src="https://contrib.rocks/image?repo=fixwire/fixwire-python" alt="Contributors" /></a>
