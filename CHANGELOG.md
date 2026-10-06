# Changelog

All notable changes to the Fixwire Python SDK are listed here. Versions follow [Semantic
Versioning](https://semver.org); before 1.0, a minor version may change the
API.

## [Unreleased]

- Fingerprinting a message with many "@" no longer hangs the caller (the email pattern backtracked cubically).
- Redaction stays linear on hostile text: JWTs are found by a scanner (same matches as the server), and many findings no longer cost quadratic time.
- A huge or infinite Retry-After or Fixwire-Rate-Limits value no longer stops delivery: waits are capped at a day.
- Answers are read up to 64 KB and redirects are never followed.
- A forked child no longer hangs on locks held at the fork, and opens its own connections.
- flush() and the asyncio hand-over no longer race the delivery thread; close() waits at most its timeout.
- Cyclic values serialize as "[Circular ~]"; only the kept items of a large list or bytes value are read.
- The logging integration ignores records logged while it reports one (before_send, event processors).
- capture_exception() and finished spans never raise into the app when an exception or attribute breaks on reading.
- The offline spool is readable by its user only (0600 files, 0700 directory); requests given up on leave it.
- Incoming baggage is capped at 8192 bytes; sessions count at most 5,000 users apart per send.
- Message stacks stop at max_stack_frames; local variables and source context stay within their budgets (no FIFOs or files over 10 MB).

## [0.1.0] - 2026-10-06

First release.

- Errors with their causes, messages, breadcrumbs and scopes; a sync client and a native asyncio client sharing one core.
- Integrations for Django, Flask, FastAPI and Starlette, Celery, logging, requests and httpx.
- Traces with OpenTelemetry; AI agent runs (OpenAI and Anthropic clients).
- Release health, cron monitors and feedback.
- On-device redaction with the server's rules; an error budget for crash loops.
- Examples run against a fake ingest in CI.
