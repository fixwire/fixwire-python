# Examples

Real apps, each with its own README, requirements and walkthrough. Each is
also run by the SDK's test suite against a fake ingest (`tests/test_examples.py`),
so they keep working.

| Example | Shows |
|---|---|
| [fastapi-shop](fastapi-shop) | ASGI middleware, per-request users and tags, handled vs unhandled errors, background tasks, logging, `before_send`, a trace per request with a custom span |
| [flask-notes](flask-notes) | `init_app()`, per-request users, unhandled vs handled errors, a streamed response, logged errors, a trace per request with a custom span |
| [django-blog](django-blog) | Django middleware, URL patterns as transactions, 404s not reported, logged errors, a trace per request |
| [celery-worker](celery-worker) | One scope and one trace per task, retries not reported, the final failure is |
| [nightly-report](nightly-report) | A cron job with a `Client`, carrying on after failures, check-ins to a monitor, the offline queue |
| [asyncio-consumer](asyncio-consumer) | `AsyncClient`, one scope per message with interleaving workers |
| [support-agent](support-agent) | An agent on Claude: agent runs, model calls with tokens, tool calls and their failures |
