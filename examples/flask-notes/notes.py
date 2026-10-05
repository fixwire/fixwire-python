"""A small notes API on Flask, instrumented with Fixwire.

Run it:  FIXWIRE_DSN=https://<key>@<host> flask --app notes run
Without FIXWIRE_DSN the SDK does nothing and the app still works.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any, cast

from flask import Flask, Response, abort, request

import fixwire
from fixwire.integrations.flask import init_app

fixwire.init(
    dsn=os.environ.get("FIXWIRE_DSN"),
    release=os.environ.get("RELEASE", "notes-api@1.0.0"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    # A trace per request, named after its URL rule; in production 0.1–0.2 is typical.
    traces_sample_rate=float(os.environ.get("TRACES_SAMPLE_RATE", "1.0")),
)

log = logging.getLogger("notes")
app = Flask("notes")
init_app(app)  # a scope and a trace per request, view errors reported

NOTES: dict[int, dict[str, Any]] = {
    1: {"title": "Groceries", "body": "beans, milk", "tags": ["home"]},
    2: {"title": "Draft", "body": None, "tags": []},  # a data bug waiting to happen
}


class MailerDown(Exception):
    """The mail provider refused: handled, still worth tracking."""


@app.before_request
def who() -> None:
    # Set per request: concurrent requests never mix users or tags.
    fixwire.set_user({"id": request.headers.get("X-User-Id", "anonymous")})


@app.get("/notes/<int:note_id>")
def show(note_id: int) -> dict[str, Any]:
    note = NOTES.get(note_id) or abort(404)  # a 404 is not an error report
    fixwire.add_breadcrumb(category="notes", message="note loaded", data={"id": note_id})
    # A bug for notes without a body: reported as unhandled, with the URL
    # rule /notes/<int:note_id> as the transaction.
    return {**note, "preview": note["body"][:20]}


@app.post("/notes/<int:note_id>/share")
def share(note_id: int) -> tuple[dict[str, str], int]:
    note = NOTES.get(note_id) or abort(404)
    body = request.get_json(silent=True)
    email = str(cast("dict[str, Any]", body).get("email", "")) if isinstance(body, dict) else ""
    try:
        with fixwire.start_span("send mail", op="mail.send"):  # a span of your own
            send_mail(email, note["title"])
    except MailerDown as e:
        # Handled: the user gets an answer, Fixwire gets the event with
        # context, for this event only. The email in the message is masked.
        with fixwire.new_scope() as scope:
            scope.set_context("share", {"note": note_id, "provider": "acme-mail"})
            scope.set_level("warning")
            fixwire.capture_exception(e)
        return {"status": "queued for retry"}, 202
    return {"status": "sent"}, 200


@app.get("/notes/export")
def export() -> Response:
    def rows() -> Iterator[str]:
        yield "id,title\n"
        for note_id, note in NOTES.items():
            yield "%d,%s\n" % (note_id, note["title"])
        log.warning("export finished for %d notes", len(NOTES))  # a breadcrumb

    # Streamed: the request's trace ends when the last row is sent.
    return Response(rows(), mimetype="text/csv")


@app.post("/admin/reindex")
def reindex() -> dict[str, str]:
    try:
        raise TimeoutError("search cluster did not answer")
    except TimeoutError:
        log.exception("reindex failed for %d notes", len(NOTES))  # logged errors become events
    return {"status": "partial"}


def send_mail(to: str, subject: str) -> None:
    if to.endswith("@bounce.example"):
        raise MailerDown("acme-mail rejected %s for %r" % (to, subject))
