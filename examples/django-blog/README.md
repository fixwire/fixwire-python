# Django blog

```sh
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt        # or, from this repo: uv pip install -e ../.. django
FIXWIRE_DSN=https://<key>@<host> python manage.py runserver
```

```sh
curl -s localhost:8000/articles/hello-fixwire/
curl -s localhost:8000/articles/missing/                   # 404: not reported
curl -s -XPOST localhost:8000/articles/draft/like/         # TypeError: reported, transaction /articles/<slug:slug>/like/
curl -s 'localhost:8000/newsletter/?email=ada@example.com'  # logged error: an event, email masked
```

Setup is two lines: `FixwireMiddleware` first in `MIDDLEWARE`, and
`fixwire.init()` at the end of `settings.py`. Works the same under WSGI
(gunicorn) and ASGI (uvicorn, daphne), with sync and async views.
