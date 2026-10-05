"""Protocol v1 on the wire: errors and messages as OTLP log records, spans
as OTLP spans, in OTLP's JSON encoding."""

from conftest import attrs

from fixwire._core import protocol

TRACE, SPAN, PARENT = "5b8efff798038103d269b633813fc60c", "eee19b7ec3c1b174", "00f067aa0ba902b7"


def test_any_value():
    assert protocol.any_value(True) == {"boolValue": True}
    assert protocol.any_value(3) == {"intValue": "3"}
    assert protocol.any_value(1 << 70) == {"stringValue": str(1 << 70)}
    assert protocol.any_value(2.5) == {"doubleValue": 2.5}
    assert protocol.any_value(float("inf")) == {"stringValue": "inf"}
    assert protocol.any_value(None) == {}
    assert protocol.any_value({"a": [1, "x"], "b": None}) == {
        "kvlistValue": {
            "values": [{"key": "a", "value": {"arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "x"}]}}}]
        }
    }
    assert protocol.any_value(object())["stringValue"].startswith("<object")


def test_a_logged_error_in_a_request():
    event = {
        "event_id": "9ec79c33ec9942ab8353589fcb2e04dc",
        "timestamp": 1791190800.25,
        "level": "warning",
        "logger": "shop.checkout",
        "logentry": {"message": "charge failed for %s", "formatted": "charge failed for ord_1"},
        "transaction": "/checkout",
        "exception": {
            "values": [
                {"type": "KeyError", "value": "'card'", "mechanism": {"type": "logging", "handled": True}},
                {
                    "type": "ChargeError",
                    "value": "declined",
                    "module": "shop.payments",
                    "mechanism": {"type": "logging", "handled": True},
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "checkout",
                                "filename": "shop/cart.py",
                                "abs_path": "/app/shop/cart.py",
                                "lineno": 20,
                                "in_app": True,
                            },
                            {
                                "function": "charge",
                                "filename": "shop/payments.py",
                                "lineno": 12,
                                "colno": 5,
                                "in_app": True,
                                "context_line": "raise ChargeError('declined')",
                            },
                        ]
                    },
                },
            ]
        },
        "contexts": {
            "trace": {"trace_id": TRACE, "span_id": SPAN, "op": "http.server"},
            "fixwire": {"suppressed": {"count": 4, "first": 1.0, "last": 2.0}},
        },
        "request": {
            "method": "POST",
            "url": "https://shop.example/checkout",
            "query_string": "step=2",
            "headers": {"user-agent": "Mozilla/5.0", "referer": "https://shop.example/cart"},
        },
        "tags": {"plan": "team"},
    }
    r = protocol.log_record(event)
    a = attrs(r.pop("attributes"))
    assert r == {
        "timeUnixNano": "1791190800250000000",
        "eventName": "exception",
        "severityNumber": 13,
        "severityText": "WARN",
        "traceId": TRACE,
        "spanId": SPAN,
        "body": {"stringValue": "charge failed for ord_1"},
    }
    assert a == {
        "exception.type": "ChargeError",
        "exception.message": "declined",
        "fixwire.exceptions": [
            {
                "type": "ChargeError",
                "message": "declined",
                "module": "shop.payments",
                "mechanism": {"type": "logging", "handled": True},
                "frames": [
                    {
                        "function": "checkout",
                        "file": "shop/cart.py",
                        "abs_path": "/app/shop/cart.py",
                        "line": 20,
                        "in_app": True,
                    },
                    {
                        "function": "charge",
                        "file": "shop/payments.py",
                        "line": 12,
                        "column": 5,
                        "in_app": True,
                        "context_line": "raise ChargeError('declined')",
                    },
                ],
            },
            {"type": "KeyError", "message": "'card'", "mechanism": {"type": "logging", "handled": True}},
        ],
        "fixwire.handled": True,
        "fixwire.event_id": "9ec79c33ec9942ab8353589fcb2e04dc",
        "fixwire.tags": {"plan": "team", "logger": "shop.checkout"},
        "fixwire.suppressed": 4,
        "fixwire.transaction": "/checkout",
        "http.request.method": "POST",
        "url.full": "https://shop.example/checkout?step=2",
        "user_agent.original": "Mozilla/5.0",
        "http.request.header.referer": "https://shop.example/cart",
    }


def test_a_message():
    r = protocol.log_record({"message": "nightly report finished", "level": "info", "timestamp": 1.0})
    assert r["eventName"] == "fixwire.message" and r["body"] == {"stringValue": "nightly report finished"}
    assert r["severityNumber"] == 9 and "traceId" not in r
    # A level the protocol doesn't know is an error's.
    assert protocol.log_record({"message": "x", "level": "loud"})["severityNumber"] == 17


def test_spans():
    record = {
        "trace_id": TRACE,
        "span_id": SPAN,
        "parent_span_id": PARENT,
        "parent_remote": True,
        "name": "GET /orders/{id}",
        "op": "http.server",
        "origin": "auto.http.asgi",
        "status": "error",
        "start": 1.5,
        "end": 2.25,
        "attributes": {"http.request.method": "GET", "http.response.status_code": 500},
    }
    s = protocol.span(record)
    assert attrs(s.pop("attributes")) == {
        "http.request.method": "GET",
        "http.response.status_code": 500,
        "fixwire.op": "http.server",
        "fixwire.origin": "auto.http.asgi",
    }
    assert s == {
        "traceId": TRACE,
        "spanId": SPAN,
        "parentSpanId": PARENT,
        "name": "GET /orders/{id}",
        "kind": protocol.SERVER,
        "startTimeUnixNano": "1500000000",
        "endTimeUnixNano": "2250000000",
        "status": {"code": 2},
        "flags": 0x301,
    }
    local = protocol.span({**record, "parent_remote": False, "status": "ok", "op": None})
    assert local["flags"] == 0x101 and local["status"] == {"code": 1} and local["kind"] == protocol.INTERNAL
    assert [protocol.span_kind(op) for op in ("db", "db.query", "http.client", "queue.publish", "gen_ai.chat")] == [
        protocol.CLIENT,
        protocol.CLIENT,
        protocol.CLIENT,
        protocol.PRODUCER,
        protocol.CLIENT,
    ]
    assert protocol.span_kind("gen_ai.execute_tool") == protocol.span_kind("task") == protocol.INTERNAL


def test_resource(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    res = attrs(protocol.resource(protocol.service_name("api@1.4.0"), "api@1.4.0", "staging", "web-1")["attributes"])
    assert res == {
        "service.name": "api",
        "service.version": "api@1.4.0",
        "deployment.environment.name": "staging",
        "host.name": "web-1",
        "telemetry.sdk.name": "fixwire.python",
        "telemetry.sdk.version": res["telemetry.sdk.version"],
        "telemetry.sdk.language": "python",
    }
    assert protocol.service_name("1.4.0") is None
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout")
    assert protocol.service_name("api@1.4.0") == "checkout"
