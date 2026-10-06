"""Framework integrations end to end against the fake ingest."""

import pytest

import fixwire

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_asgi_starlette(ingest):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from fixwire.integrations.asgi import FixwireMiddleware

    async def ok(request):
        fixwire.set_tag("route", "ok")
        return PlainTextResponse("ok")

    async def item(request):
        fixwire.set_tag("item", request.path_params["id"])
        raise ValueError("item %s is broken" % request.path_params["id"])

    fixwire.init(ingest.dsn, default_integrations=False)
    app = Starlette(routes=[Route("/ok", ok), Route("/items/{id}", item)])
    app.add_middleware(FixwireMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/ok").status_code == 200
        r = client.get(
            "/items/42?color=red",
            headers={"Cookie": "session=secret", "Authorization": "Bearer t", "User-Agent": "pytest"},
        )
        assert r.status_code == 500
    assert fixwire.flush(5)
    [event] = ingest.events()
    assert event["transaction"] == "/items/{id}"
    assert event["tags"] == {"item": "42"}  # the /ok request's tag stayed in its own scope
    assert event["request"] == {
        "method": "GET",
        "url": "http://testserver/items/42?color=red",
        "headers": {"User-Agent": "pytest"},
    }
    assert event["exception"]["values"][-1]["mechanism"] == {"type": "asgi", "handled": False}
    [(record, _)] = ingest.records()
    a = record["attributes"]
    assert a["user_agent.original"] == "pytest" and a["http.request.method"] == "GET"
    # Never cookies or authorization.
    assert "secret" not in str(ingest.requests[0]["body"]) and "Bearer t" not in str(ingest.requests[0]["body"])


def test_asgi_mounted_routes_keep_their_prefix(ingest):
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    from fixwire.integrations.asgi import FixwireMiddleware

    async def broken(request):
        raise ValueError("broken")

    fixwire.init(ingest.dsn, default_integrations=False)
    v2 = Mount("/v2", routes=[Route("/orders/{id}", broken)])
    app = Starlette(routes=[Mount("/api", routes=[Route("/items/{id}", broken), v2])])
    app.add_middleware(FixwireMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/items/7").status_code == 500
        assert client.get("/api/v2/orders/9").status_code == 500
    assert fixwire.flush(5)
    events = ingest.events()
    assert [e["transaction"] for e in events] == ["/api/items/{id}", "/api/v2/orders/{id}"]
    assert [e["request"]["url"] for e in events] == [
        "http://testserver/api/items/7",
        "http://testserver/api/v2/orders/9",
    ]


def test_django(ingest, settings_module):
    from django.test import Client as DjangoClient

    fixwire.init(ingest.dsn, default_integrations=False)
    r = DjangoClient(raise_request_exception=False).get("/orders/7/", HTTP_USER_AGENT="pytest", HTTP_COOKIE="sid=1")
    assert r.status_code == 500
    assert fixwire.flush(5)
    [event] = ingest.events()
    assert event["transaction"] == "/orders/<int:order_id>/"
    assert event["tags"]["order"] == "7"
    assert event["request"]["url"] == "http://testserver/orders/7/"
    assert event["request"]["headers"] == {"User-Agent": "pytest"}  # what the crawler filter reads
    assert "sid=1" not in str(ingest.requests[0]["body"])
    assert event["exception"]["values"][-1]["mechanism"]["type"] == "django"


@pytest.fixture
def settings_module():
    import sys
    import types

    import django
    from django.conf import settings
    from django.http import HttpResponse
    from django.urls import path

    def order(request, order_id):
        fixwire.set_tag("order", order_id)
        raise RuntimeError("order %d failed" % order_id)

    def home(request):
        return HttpResponse("ok")

    urls = types.ModuleType("fixwire_test_urls")
    urls.urlpatterns = [path("", home), path("orders/<int:order_id>/", order)]
    sys.modules["fixwire_test_urls"] = urls
    if not settings.configured:
        settings.configure(
            DEBUG=False,
            SECRET_KEY="x",
            ALLOWED_HOSTS=["*"],
            ROOT_URLCONF="fixwire_test_urls",
            MIDDLEWARE=["fixwire.integrations.django.FixwireMiddleware"],
            LOGGING_CONFIG=None,
        )
        django.setup()
    yield


def test_celery(ingest):
    from celery import Celery

    from fixwire.integrations.celery import CeleryIntegration

    fixwire.init(ingest.dsn, default_integrations=False, integrations=[CeleryIntegration()])
    app = Celery("shop", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    @app.task(name="shop.charge")
    def charge(order):
        fixwire.set_tag("order", order)
        raise ValueError("charge for %s failed" % order)

    charge.delay("ord_1")
    charge.delay("ord_2")
    assert fixwire.flush(5)
    events = ingest.events()
    assert sorted(e["tags"]["order"] for e in events) == ["ord_1", "ord_2"]
    e = events[0]
    assert e["transaction"] == "shop.charge" and e["tags"]["celery_task"] == "shop.charge"
    assert e["contexts"]["celery"]["task"] == "shop.charge"
    assert e["exception"]["values"][-1]["mechanism"] == {"type": "celery", "handled": False}


def test_flask(ingest):
    import logging

    TRACE, PARENT = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"

    from flask import Flask, abort

    from fixwire.integrations.flask import init_app

    fixwire.init(ingest.dsn, traces_sample_rate=1.0)  # default integrations: logging too
    app = Flask("shop")
    init_app(app)

    @app.get("/orders/<int:order_id>")
    def order(order_id: int):
        fixwire.set_tag("order", str(order_id))
        if order_id == 404:
            abort(404)
        return {"items": [1, 2]}[order_id]  # KeyError for unknown orders

    @app.get("/export")
    def export():
        def rows():
            yield "id\n"
            yield "1\n"

        return app.response_class(rows(), mimetype="text/csv")

    client = app.test_client()

    def get(path, **kw):
        # As a server does: send the whole body, then close the response.
        with client.get(path, **kw) as r:
            return r.status_code, r.data

    assert get("/orders/7", headers={"traceparent": "00-%s-%s-01" % (TRACE, PARENT)})[0] == 500
    assert get("/orders/404")[0] == 404
    assert get("/export") == (200, b"id\n1\n")
    logging.getLogger().handlers[:] = [h for h in logging.getLogger().handlers if type(h).__name__ != "FixwireHandler"]
    assert fixwire.flush(5)

    [event] = ingest.events()  # once: Flask's own log of the error is not sent again
    assert event["exception"]["values"][-1]["type"] == "KeyError"
    assert event["exception"]["values"][-1]["mechanism"] == {"type": "flask", "handled": False}
    assert event["transaction"] == "/orders/<int:order_id>" and event["tags"]["order"] == "7"
    assert event["request"]["url"].endswith("/orders/7") and event["request"]["method"] == "GET"
    assert event["contexts"]["trace"]["trace_id"] == TRACE
    segments = [s for s in ingest.spans() if s["is_segment"]]
    orders = [s for s in segments if s["name"] == "GET /orders/<int:order_id>"]
    assert sorted(s["attributes"]["http.response.status_code"] for s in orders) == [404, 500]
    failed = next(s for s in orders if s["status"] == "error")
    assert failed["trace_id"] == TRACE and failed["parent_span_id"] == PARENT
    assert failed["attributes"]["http.route"] == "/orders/<int:order_id>"
    assert next(s for s in segments if s["name"] == "GET /export")["status"] == "ok"


def test_plain_wsgi_app(ingest):
    from werkzeug.test import Client

    from fixwire.integrations.wsgi import FixwireMiddleware

    fixwire.init(ingest.dsn, traces_sample_rate=1.0, default_integrations=False)

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/boom":
            raise ConnectionError("warehouse unreachable")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    client = Client(FixwireMiddleware(app))
    assert client.get("/ok").data == b"ok"
    with pytest.raises(ConnectionError):
        client.get("/boom?debug=1")
    assert fixwire.flush(5)
    [event] = ingest.events()
    assert event["exception"]["values"][-1]["mechanism"] == {"type": "wsgi", "handled": False}
    assert event["transaction"] == "/boom" and event["request"]["url"].endswith("/boom?debug=1")
    segments = {s["name"]: s for s in ingest.spans() if s["is_segment"]}
    assert segments["GET /ok"]["attributes"]["http.response.status_code"] == 200
    assert segments["GET /boom"]["attributes"]["http.response.status_code"] == 500
