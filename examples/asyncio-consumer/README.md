# asyncio consumer

```sh
uv venv && source .venv/bin/activate
uv pip install "fixwire[async]"        # or, from this repo: uv pip install -e "../..[async]"
FIXWIRE_DSN=https://<key>@<host> python consumer.py
```

Two workers consume four messages; two are invalid. Each failure is
reported with its own `order` tag and message context: scopes are per
message, even with workers interleaving on one loop. Delivery is a task on
the loop (httpx), never a thread blocking it.
