# Upstream

A hard fork: we don't merge upstream, we cherry-pick fixes by hand.

| Upstream | Base | Ported into |
|---|---|---|
| getsentry/sentry-python | 8afefe8 (2.71.0) | `fixwire/_core/event_builder.py` (exception chains, ExceptionGroup, frames, in-app rules) |

To review upstream changes to a ported path since the base:

```sh
git -C sentry-python log --oneline 8afefe8..HEAD -- sentry_sdk/utils.py
```
