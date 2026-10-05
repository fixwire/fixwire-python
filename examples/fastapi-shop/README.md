# FastAPI shop

A small shop API: products, a cart and a checkout, with the failures real
shops have.

```sh
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt        # or, from this repo: uv pip install -e ../.. fastapi uvicorn
FIXWIRE_DSN=https://<key>@<host> uvicorn app:app --reload
```

Try it:

```sh
curl -s localhost:8000/products
curl -s -XPOST localhost:8000/cart/items -H 'X-User-Id: u-42' -H 'X-Plan: pro' -H 'content-type: application/json' -d '{"sku":"sku-1","quantity":2}'
# declined card: a handled warning, card number and email masked
curl -s -XPOST localhost:8000/checkout -H 'X-User-Id: u-42' -H 'content-type: application/json' -d '{"card_number":"4000 0000 0000 0002","email":"ada@example.com"}'
# gateway timeout: an unhandled error, reported by the middleware
curl -s -XPOST localhost:8000/cart/items -H 'X-User-Id: u-7' -H 'content-type: application/json' -d '{"sku":"sku-1"}'
curl -s -XPOST localhost:8000/checkout -H 'X-User-Id: u-7' -H 'content-type: application/json' -d '{"card_number":"4000 0000 0000 0009","email":"bob@example.com"}'
curl -s -XPOST localhost:8000/admin/sync-inventory   # a logged error
```

What each part shows:

| Code | What you get in Fixwire |
|---|---|
| `app.add_middleware(FixwireMiddleware)` | Unhandled errors with the request (allowlisted headers only) and the route (`/checkout`) as the transaction |
| `current_user()` dependency | User and tags set per request; concurrent requests never mix |
| `new_scope()` around a handled error | Extra context and a level for one event only |
| `add_breadcrumb()` | The steps before the error, within the request (each request keeps its own) |
| `background.add_task(send_receipt, …)` | Failures after the response are reported too |
| `log.exception(…)` | Logged errors become events; `log.info` lines become breadcrumbs |
| `before_send=drop_noise` | The last word on what is sent |

Secrets and personal data (the card number, the email in the error message)
are masked on this machine before anything is sent, with the same rules as
the Fixwire server.
