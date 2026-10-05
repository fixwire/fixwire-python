# Flask notes API

A small notes API with the failures real apps have.

```sh
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt        # or, from this repo: uv pip install -e ../.. flask
FIXWIRE_DSN=https://<key>@<host> flask --app notes run
```

Try it:

```sh
curl -s localhost:5000/notes/1 -H 'X-User-Id: u-42'     # fine
curl -s localhost:5000/notes/9                           # 404: not reported
curl -s localhost:5000/notes/2 -H 'X-User-Id: u-7'      # TypeError: reported, transaction /notes/<int:note_id>
curl -s -XPOST localhost:5000/notes/1/share -H 'content-type: application/json' -d '{"email":"ada@bounce.example"}'
curl -s localhost:5000/notes/export                      # a streamed response, traced to the last row
curl -s -XPOST localhost:5000/admin/reindex              # a logged error
```

| Code | What you get in Fixwire |
|---|---|
| `init_app(app)` | A scope and a trace per request, named after the URL rule; view errors reported once (Flask's own log of them isn't sent twice) |
| `before_request` with `set_user` | The user per request; concurrent requests never mix |
| `start_span("send mail")` | A span inside the request's trace |
| `new_scope()` around a handled error | Context and a level for one event only; the email is masked |
| `log.exception(...)` | Logged errors become events |
