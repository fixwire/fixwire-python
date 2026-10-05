"""Release health: each request (and serverless invocation) is a session,
counted per minute and user and sent as aggregates; users only as hashes."""

import re

import pytest

import fixwire
from fixwire._core.sessions import Aggregates, hash_identity
from fixwire.integrations.wsgi import FixwireMiddleware


def sessions(ingest):
    return ingest.bodies("/v1/sessions")


def totals(batches):
    out = {"exited": 0, "errored": 0, "crashed": 0}
    for b in batches:
        for a in b["aggregates"]:
            for k in out:
                out[k] += a.get(k, 0)
    return out


def request(user=None, error=None):
    client = fixwire.get_client()
    with fixwire.isolation_scope() as iso:
        end = client.start_request_session()
        if user:
            iso.set_user({"id": user})
        if error == "handled":
            fixwire.capture_exception(ValueError("retrying"))
        elif error == "unhandled":
            client.capture_exception(ValueError("boom"), mechanism={"type": "http", "handled": False})
        end()
        end()  # ending twice counts once


def test_requests_are_sessions_counted_per_minute_and_user(ingest):
    fixwire.init(ingest.dsn, release="api@2.0.0", environment="staging", default_integrations=False)
    request("u1")
    request("u1", "handled")
    request("u2", "unhandled")
    request()
    assert fixwire.flush(5)
    batches = sessions(ingest)
    assert len(batches) == 1
    assert batches[0]["release"] == "api@2.0.0" and batches[0]["environment"] == "staging"
    assert batches[0]["sdk"]["name"] == "fixwire.python" and "sessions" not in batches[0]
    assert totals(batches) == {"exited": 2, "errored": 1, "crashed": 1}
    dids = {a["did"] for a in batches[0]["aggregates"] if "did" in a}
    assert dids == {hash_identity("u1"), hash_identity("u2")}
    assert all(re.fullmatch(r"[0-9a-f]{32}", d) for d in dids)
    assert "u1" not in str(batches), "users leave only as hashes"


@pytest.mark.parametrize("options", [{}, {"release": "api@2", "auto_session_tracking": False}])
def test_sessions_need_a_release_and_can_be_turned_off(ingest, options):
    fixwire.init(ingest.dsn, default_integrations=False, **options)
    request("u1")
    fixwire.flush(2)
    assert sessions(ingest) == []


def test_a_wsgi_app_counts_its_requests(ingest):
    fixwire.init(ingest.dsn, release="web@1.0.0", default_integrations=False)

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        if path == "/boom":
            raise RuntimeError("boom")
        if path == "/handled":
            fixwire.capture_exception(ValueError("handled"))
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    wrapped = FixwireMiddleware(app)
    for path in ("/ok", "/ok", "/handled", "/boom"):
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "wsgi.url_scheme": "http",
            "SERVER_NAME": "x",
            "SERVER_PORT": "80",
        }
        try:
            body = wrapped(environ, lambda status, headers, exc_info=None: None)
            list(body)
            body.close()
        except RuntimeError:
            pass
    assert fixwire.flush(5)
    assert totals(sessions(ingest)) == {"exited": 2, "errored": 1, "crashed": 1}


def test_a_serverless_invocation_is_a_session(ingest):
    fixwire.init(ingest.dsn, release="fn@1.0.0", default_integrations=False)

    @fixwire.serverless_function
    def handler(event, context):
        if event.get("fail"):
            raise ValueError("declined")
        return "ok"

    assert handler({}, None) == "ok"
    with pytest.raises(ValueError):
        handler({"fail": True}, None)
    fixwire.flush(5)
    assert totals(sessions(ingest)) == {"exited": 1, "errored": 0, "crashed": 1}


def test_aggregates_send_once_a_minute():
    agg = Aggregates()
    assert agg.record("ok", "u", 1000.0) is False
    assert agg.record("ok", "u", 1030.0) is False
    assert agg.record("crashed", "u", 1061.0) is True  # due: sent with the next queue pass
    assert agg.record("ok", None, 1062.0) is False  # already due, not twice
    assert len(agg) == 3  # minutes 960, 1020 and 1060 (one with a user, one without)
    aggregates = agg.take()
    assert aggregates is not None and len(aggregates) == 3 and len(agg) == 0
    assert aggregates[0] == {"started": "1970-01-01T00:16:00Z", "did": hash_identity("u"), "exited": 1}
    assert agg.take() is None
