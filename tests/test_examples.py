"""The examples are real apps: run each against the fake ingest and check
what Fixwire receives, so they keep working."""

import importlib.util
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

import fixwire

EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(script: str, dsn: str, cwd: pathlib.Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "FIXWIRE_DSN": dsn, **(env or {})},
    )


def messages(events):
    out = []
    for e in events:
        if "exception" in e:
            out.append("%s: %s" % (e["exception"]["values"][-1]["type"], e["exception"]["values"][-1]["value"]))
        else:
            out.append(e.get("message"))
    return out


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_fastapi_shop(ingest, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("FIXWIRE_DSN", ingest.dsn)
    shop = load("example_fastapi_shop", EXAMPLES / "fastapi-shop" / "app.py")
    with TestClient(shop.app, raise_server_exceptions=False) as client:
        alice = {"X-User-Id": "u-42", "X-Plan": "pro"}
        assert client.post("/cart/items", json={"sku": "nope"}, headers=alice).status_code == 404
        assert client.post("/cart/items", json={"sku": "sku-1", "quantity": 2}, headers=alice).status_code == 200
        r = client.post(
            "/checkout", json={"card_number": "4000 0000 0000 0002", "email": "ada@example.com"}, headers=alice
        )
        assert r.status_code == 402
        bob = {"X-User-Id": "u-7"}
        client.post("/cart/items", json={"sku": "sku-1"}, headers=bob)
        assert (
            client.post(
                "/checkout", json={"card_number": "4000 0000 0000 0009", "email": "bob@example.com"}, headers=bob
            ).status_code
            == 500
        )
        carol = {"X-User-Id": "u-9"}
        client.post("/cart/items", json={"sku": "sku-1"}, headers=carol)
        client.post(
            "/checkout", json={"card_number": "4242 4242 4242 4242", "email": "carol@bounce.example"}, headers=carol
        )
        assert client.post("/admin/sync-inventory").status_code == 200
    assert fixwire.flush(5)
    events = {e["exception"]["values"][-1]["type"]: e for e in ingest.events()}
    assert set(events) == {"PaymentDeclined", "GatewayTimeout", "RuntimeError", "TimeoutError"}

    declined = events["PaymentDeclined"]
    assert declined["level"] == "warning" and declined["user"] == {"id": "u-42"} and declined["tags"]["plan"] == "pro"
    assert declined["exception"]["values"][-1]["value"] == "card [REDACTED:credit_card] declined for 29.80 EUR"
    assert declined["contexts"]["order"] == {"total": "29.80", "items": 1}
    # Breadcrumbs are per request: the cart request's crumb stayed with it.
    crumbs = [b["message"] for b in declined["breadcrumbs"]["values"]]
    assert crumbs[-1] == "checkout started" and "added sku-1 × 2" not in crumbs

    timeout = events["GatewayTimeout"]
    assert timeout["level"] == "fatal" and timeout["transaction"] == "/checkout" and timeout["user"] == {"id": "u-7"}
    assert "plan" not in timeout["tags"] or timeout["tags"]["plan"] == "free"  # Alice's tags stayed with Alice

    assert events["RuntimeError"]["exception"]["values"][-1]["value"] == "receipt for [REDACTED:email] bounced"
    assert events["TimeoutError"]["message"] == "inventory sync failed for 2 products"

    # Traces: a segment per request, named after its route, with the charge inside.
    spans = ingest.spans()
    checkouts = [s for s in spans if s["is_segment"] and s["name"] == "POST /checkout"]
    assert sorted(s["attributes"]["http.response.status_code"] for s in checkouts) == [200, 402, 500]
    failed = next(s for s in checkouts if s["status"] == "error")
    charge = next(s for s in spans if s.get("parent_span_id") == failed["span_id"] and s["name"] == "charge card")
    assert charge["status"] == "error" and charge["attributes"]["fixwire.op"] == "payment"
    assert timeout["contexts"]["trace"]["trace_id"] == failed["trace_id"]


def test_django_blog(ingest):
    r = run(
        """
        import os, sys
        os.environ["DJANGO_SETTINGS_MODULE"] = "blog.settings"
        sys.path.insert(0, ".")
        import django; django.setup()
        from django.test import Client
        c = Client(raise_request_exception=False)
        assert c.get("/articles/hello-fixwire/").status_code == 200
        assert c.get("/articles/missing/").status_code == 404
        assert c.post("/articles/draft/like/").status_code == 500
        assert c.get("/newsletter/?email=ada@example.com").status_code == 202
        import fixwire; assert fixwire.flush(5)
        """,
        ingest.dsn,
        EXAMPLES / "django-blog",
    )
    assert r.returncode == 0, r.stderr
    events = ingest.events()
    assert sorted(messages(events)) == [
        "ConnectionError: mail provider rejected [REDACTED:email]",
        "TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'",
    ]
    like = next(e for e in events if e["exception"]["values"][-1]["type"] == "TypeError")
    assert like["transaction"] == "/articles/<slug:slug>/like/" and like["tags"]["article"] == "draft"
    assert like["breadcrumbs"]["values"][-1]["message"] == "like clicked"
    segments = {s["name"]: s for s in ingest.spans() if s["is_segment"]}
    assert segments["GET /articles/<slug:slug>/"]["status"] == "ok"
    assert segments["POST /articles/<slug:slug>/like/"]["status"] == "error"
    assert like["contexts"]["trace"]["trace_id"] == segments["POST /articles/<slug:slug>/like/"]["trace_id"]


def test_celery_worker(ingest, monkeypatch):
    monkeypatch.setenv("FIXWIRE_DSN", ingest.dsn)
    monkeypatch.syspath_prepend(str(EXAMPLES / "celery-worker"))
    tasks = load("tasks", EXAMPLES / "celery-worker" / "tasks.py")
    tasks.app.conf.update(task_always_eager=True, broker_url="memory://")
    for account in ("acme", "globex", "closed-initech"):
        tasks.generate_invoice.delay(account, "2026-10")
    assert fixwire.flush(5)
    [event] = ingest.events()
    assert messages([event]) == ["LookupError: account closed-initech has no billing address"]
    assert event["transaction"] == "billing.generate_invoice" and event["tags"]["account"] == "closed-initech"
    assert event["breadcrumbs"]["values"][-1]["data"] == {"month": "2026-10"}
    spans = ingest.spans()
    runs = [s for s in spans if s["is_segment"] and s["attributes"]["fixwire.op"] == "queue.process"]
    assert len(runs) == 3 and {s["name"] for s in runs} == {"billing.generate_invoice"}
    failed = [s for s in runs if s["status"] == "error"]
    assert len(failed) == 1 and failed[0]["trace_id"] == event["contexts"]["trace"]["trace_id"]
    assert sum(1 for s in spans if s["name"] == "render PDF") == 3


def test_nightly_report(ingest, tmp_path):
    r = run(
        "import runpy; runpy.run_path('report.py', run_name='__main__')",
        ingest.dsn,
        EXAMPLES / "nightly-report",
        {"XDG_CACHE_HOME": str(tmp_path)},
    )
    assert r.returncode == 1  # one account failed; the job carried on
    assert "acme: 120 rows" in r.stdout and "umbrella: 4 rows" in r.stdout
    events = ingest.events()
    assert sorted(messages(events)) == ["KeyError: 'initech'", "nightly report finished with 1 failures"]
    assert next(e for e in events if "exception" in e)["tags"]["account"] == "initech"
    started, finished = ingest.bodies("/v1/check-ins/nightly-report")
    assert started["status"] == "in_progress" and started["monitor_config"]["schedule"]["value"] == "0 3 * * *"
    assert finished["status"] == "error" and finished["check_in_id"] == started["check_in_id"]
    assert finished["duration"] >= 0


def test_asyncio_consumer(ingest):
    r = run(
        "import runpy; runpy.run_path('consumer.py', run_name='__main__')", ingest.dsn, EXAMPLES / "asyncio-consumer"
    )
    assert r.returncode == 0, r.stderr
    events = ingest.events()
    assert sorted(e["tags"]["order"] for e in events) == ["ord_1", "ord_3"]
    assert all(e["contexts"]["message"]["id"] == e["tags"]["order"] for e in events)


def test_support_agent(ingest, monkeypatch):
    from types import SimpleNamespace as NS

    monkeypatch.setenv("FIXWIRE_DSN", ingest.dsn)
    agent = load("example_support_agent", EXAMPLES / "support-agent" / "agent.py")

    def reply(stop, *blocks, tokens=(900, 60)):
        usage = NS(
            input_tokens=tokens[0], output_tokens=tokens[1], cache_read_input_tokens=800, cache_creation_input_tokens=0
        )
        return NS(
            id="msg_%s" % stop, model="claude-opus-5-5-20261001", stop_reason=stop, content=list(blocks), usage=usage
        )

    script = [
        reply("tool_use", NS(type="tool_use", id="toolu_1", name="lookup_order", input={"order_id": "ord_1"})),
        reply(
            "tool_use",
            NS(type="tool_use", id="toolu_2", name="refund_order", input={"order_id": "ord_1", "reason": "broken"}),
        ),
        reply(
            "end_turn",
            NS(type="text", text="Your order has shipped, so I can't refund it; I've asked DHL to collect it."),
            tokens=(1400, 30),
        ),
    ]
    sent = []

    class Messages:
        def create(self, **params):
            sent.append(params)
            return script[len(sent) - 1]

    client = fixwire.ai.wrap_anthropic(NS(messages=Messages()))
    answer = agent.answer(client, "Refund ord_1, it arrived broken")
    assert fixwire.flush(5)
    assert answer.startswith("Your order has shipped")
    assert len(sent) == 3 and sent[0]["model"] == "claude-opus-5-5" and sent[0]["tools"] == agent.TOOLS

    spans = ingest.spans()
    op = lambda s: s["attributes"]["fixwire.op"]  # noqa: E731
    value = lambda s, k: s["attributes"].get(k)  # noqa: E731
    [run] = [s for s in spans if op(s) == "gen_ai.invoke_agent"]
    assert run["is_segment"] and value(run, "gen_ai.agent.name") == "support-agent"
    assert value(run, "gen_ai.input.messages") is None  # content stays out by default
    chats = [s for s in spans if op(s) == "gen_ai.chat"]
    assert len(chats) == 3 and all(s["parent_span_id"] == run["span_id"] for s in chats)
    assert sum(value(s, "gen_ai.usage.input_tokens") for s in chats) == 5600  # (900 + 800) × 2 + (1400 + 800)
    assert [value(s, "gen_ai.response.finish_reasons") for s in chats] == [
        '["tool_use"]',
        '["tool_use"]',
        '["end_turn"]',
    ]
    tools = {value(s, "gen_ai.tool.name"): s for s in spans if op(s) == "gen_ai.execute_tool"}
    assert tools["lookup_order"]["status"] == "ok"
    refund = tools["refund_order"]
    assert refund["status"] == "error" and value(refund, "error.type") == "AlreadyShipped"
    assert value(refund, "gen_ai.tool.call.id") == "toolu_2" and value(refund, "gen_ai.agent.name") == "support-agent"
    assert value(refund, "fixwire.tool.arguments_hash") == fixwire.ai.arguments_hash(
        {"order_id": "ord_1", "reason": "broken"}
    )


def test_flask_notes(ingest, monkeypatch):
    monkeypatch.setenv("FIXWIRE_DSN", ingest.dsn)
    notes = load("example_flask_notes", EXAMPLES / "flask-notes" / "notes.py")
    client = notes.app.test_client()

    def call(method, path, **kw):
        # As a server does: send the whole body, then close the response.
        with client.open(path, method=method, **kw) as r:
            return r.status_code, r.data

    assert call("GET", "/notes/1", headers={"X-User-Id": "u-42"})[0] == 200
    assert call("GET", "/notes/9")[0] == 404
    assert call("GET", "/notes/2", headers={"X-User-Id": "u-7"})[0] == 500
    assert call("POST", "/notes/1/share", json={"email": "ada@bounce.example"}) == (
        202,
        b'{"status":"queued for retry"}\n',
    )
    assert call("GET", "/notes/export")[1] == b"id,title\n1,Groceries\n2,Draft\n"
    assert call("POST", "/admin/reindex")[0] == 200
    assert fixwire.flush(5)

    events = ingest.events()
    assert sorted(messages(events)) == [
        "MailerDown: acme-mail rejected [REDACTED:email] for 'Groceries'",
        "TimeoutError: search cluster did not answer",
        "TypeError: 'NoneType' object is not subscriptable",
    ]
    bug = next(e for e in events if e["exception"]["values"][-1]["type"] == "TypeError")
    assert bug["transaction"] == "/notes/<int:note_id>" and bug["user"] == {"id": "u-7"}
    assert bug["exception"]["values"][-1]["mechanism"]["type"] == "flask"
    assert bug["breadcrumbs"]["values"][-1]["message"] == "note loaded"
    shared = next(e for e in events if e["exception"]["values"][-1]["type"] == "MailerDown")
    assert shared["level"] == "warning" and shared["contexts"]["share"]["provider"] == "acme-mail"

    segments = [s for s in ingest.spans() if s["is_segment"]]
    by_name = {}
    for s in segments:
        by_name.setdefault(s["name"], []).append(s)
    assert sorted(s["attributes"]["http.response.status_code"] for s in by_name["GET /notes/<int:note_id>"]) == [
        200,
        404,
        500,
    ]
    share = by_name["POST /notes/<int:note_id>/share"][0]
    assert any(s["name"] == "send mail" and s.get("parent_span_id") == share["span_id"] for s in ingest.spans())
    assert by_name["GET /notes/export"][0]["status"] == "ok"
    assert bug["contexts"]["trace"]["trace_id"] in {
        s["trace_id"] for s in by_name["GET /notes/<int:note_id>"] if s["status"] == "error"
    }
