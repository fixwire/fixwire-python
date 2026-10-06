# Changelog

All notable changes to the Fixwire Python SDK are listed here. Versions follow [Semantic
Versioning](https://semver.org); before 1.0, a minor version may change the
API.

## [0.1.0] - 2026-10-06

First release.

- Errors with their causes, messages, breadcrumbs and scopes; a sync client and a native asyncio client sharing one core.
- Integrations for Django, Flask, FastAPI and Starlette, Celery, logging, requests and httpx.
- Traces with OpenTelemetry; AI agent runs (OpenAI and Anthropic clients).
- Release health, cron monitors and feedback.
- On-device redaction with the server's rules; an error budget for crash loops.
- Examples run against a fake ingest in CI.
