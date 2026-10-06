# Changelog

All notable changes to the Fixwire Python SDK are listed here. Versions follow [Semantic
Versioning](https://semver.org); before 1.0, a minor version may change the
API.

## [0.1.1] - 2026-10-06

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
- Message stacks stop at max_stack_frames; local variables and source context stay within their budgets (no FIFOs or files over 10 MB).
- Redaction follows the server's new secret_assignment rule (names ending a longer one: access_token, client_secret, csrfToken, PHPSESSID, X-Amz-Signature; secret and private keys, credentials, session ids, signatures; an OAuth code in a query or fragment), by a linear scanner; keys that mask alike are numbered in linear time. A query is redacted as part of its URL.
- A string redaction fails on is sent as "[Filtered]", never unmasked.
- Feedback (message, name, email, URL) is redacted, then cut to max_value_length. The app's own configuration (release, environment, service and server names, monitor slugs and config) is cut to max_value_length but sent as given, never redacted (a server name looking like an email was masked).
- init() never raises: a broken DSN or option (an unknown one, a sample_rate outside 0 to 1, a negative max_breadcrumbs, transport="asyncio" outside an event loop) is said in a warning on stderr, debug or not, and the SDK stays off. Client() and AsyncClient() don't raise on one either.
- max_value_length counts UTF-8 bytes, cuts on a character boundary with "..." inside the limit, and applies to span names, ops and attributes too (recorded AI content keeps 16 kB). Redaction runs before the cut, over the part kept and the next 16 kB, so a key or token the cut goes through is masked whole.
- Values: one level past 10 is "[Object]" or "[Array]", at most 10,000 containers are walked per value, a value that can't be read is "[Unreadable]", and NaN and the infinities are "NaN", "Infinity" and "-Infinity".
- An exception chain keeps 10 exceptions; source lines come through a bounded cache (64 files, 32 MB).
- A span keeps 128 attributes; spans go in requests of at most 100; a sessions request holds at most 5,000 aggregates, and sessions count 5,000 users apart per send.
- An error over 1 MB leaves out its breadcrumbs, then its frames' variables, then its contexts, and is dropped if still over.
- Retries: a request is sent at most 4 times, a 429's retry included, again after about 1, 2 and 4 s; a request whose next try is more than 5 minutes away is dropped. Retry-After may be an HTTP date; a 429 without Fixwire-Rate-Limits pauses all data for at least 60 s, a 5xx with Retry-After for that long; unknown rate-limit categories are ignored.
- max_queue_size defaults to 100 requests, and as many may wait for a retry; past it new data is dropped (not the oldest).
- An incoming tracestate over 512 bytes or baggage over 8,192 bytes, or either with a control character, is not passed on at all; traceparent is read strictly.
- trace_propagation_targets match hosts and their subdomains (not substrings of the URL), URL prefixes, and regexes, against the URL without user info, query and fragment.
- AsyncClient.flush() and close() keep to their timeout even while the loop is busy; failures in traces_sampler and before_breadcrumb are logged.

## [0.1.0] - 2026-10-06

First release.

- Errors with their causes, messages, breadcrumbs and scopes; a sync client and a native asyncio client sharing one core.
- Integrations for Django, Flask, FastAPI and Starlette, Celery, logging, requests and httpx.
- Traces with OpenTelemetry; AI agent runs (OpenAI and Anthropic clients).
- Release health, cron monitors and feedback.
- On-device redaction with the server's rules; an error budget for crash loops.
- Examples run against a fake ingest in CI.
