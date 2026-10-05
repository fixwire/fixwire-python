"""A small shop API instrumented with Fixwire.

Run it:  FIXWIRE_DSN=https://<key>@<host> uvicorn app:app --reload
Without FIXWIRE_DSN the SDK does nothing and the shop still works.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from decimal import Decimal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import fixwire
from fixwire.integrations.asgi import FixwireMiddleware
from fixwire.types import Event, Hint


class PaymentDeclined(Exception):
    """The card was refused: expected, handled, still worth tracking."""


class GatewayTimeout(Exception):
    """The payment provider didn't answer: a real incident."""


def drop_noise(event: Event, hint: Hint) -> Event | None:
    """before_send: the last word on what leaves the app. Here, clients
    hanging up mid-request are not errors worth an alert. (Event and Hint
    are TypedDicts: their keys complete in the editor.)"""
    exc_info = hint.get("exc_info")
    if exc_info is not None and isinstance(exc_info[1], ConnectionResetError):
        return None
    return event


fixwire.init(
    dsn=os.environ.get("FIXWIRE_DSN"),
    release=os.environ.get("RELEASE", "shop-api@1.0.0"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    before_send=drop_noise,
    # A trace per request, named after its route; in production 0.1–0.2 is typical.
    traces_sample_rate=float(os.environ.get("TRACES_SAMPLE_RATE", "1.0")),
)
fixwire.set_tag("service", "shop-api")

log = logging.getLogger("shop")
logging.basicConfig(level=logging.INFO)


@dataclass
class Product:
    name: str
    price: Decimal
    stock: int


PRODUCTS = {
    "sku-1": Product("Espresso beans", Decimal("14.90"), 12),
    "sku-2": Product("Pour-over kettle", Decimal("49.00"), 0),
}
CARTS: dict[str, dict[str, int]] = {}

app = FastAPI(title="Shop")
app.add_middleware(FixwireMiddleware)


def current_user(x_user_id: str = Header(default="anonymous"), x_plan: str = Header(default="free")) -> str:
    """Who is asking. Runs inside the request's own scope, so the user and
    tags set here stay with this request only."""
    fixwire.set_user({"id": x_user_id})
    fixwire.set_tag("plan", x_plan)
    return x_user_id


class CartItem(BaseModel):
    sku: str
    quantity: int = 1


class Checkout(BaseModel):
    card_number: str
    email: str


@app.get("/products")
def products() -> dict[str, dict[str, object]]:
    return {sku: {"name": p.name, "price": str(p.price), "in_stock": p.stock > 0} for sku, p in PRODUCTS.items()}


@app.post("/cart/items")
def add_to_cart(item: CartItem, user: str = Depends(current_user)) -> dict[str, dict[str, int]]:
    product = PRODUCTS.get(item.sku)
    if product is None:
        raise HTTPException(404, "no such product")  # a 404 is not an error report
    fixwire.add_breadcrumb(category="cart", message="added %s × %d" % (item.sku, item.quantity), level="info")
    cart = CARTS.setdefault(user, {})
    cart[item.sku] = cart.get(item.sku, 0) + item.quantity
    return {"items": cart}


@app.post("/checkout")
def checkout(body: Checkout, background: BackgroundTasks, user: str = Depends(current_user)) -> dict[str, str]:
    cart = CARTS.get(user) or {}
    if not cart:
        raise HTTPException(400, "the cart is empty")
    fixwire.add_breadcrumb(category="checkout", message="checkout started", data={"items": len(cart)})
    total = sum((PRODUCTS[sku].price * n for sku, n in cart.items()), Decimal(0))
    try:
        # A span of your own: the payment call shows up inside the request's trace.
        with fixwire.start_span("charge card", op="payment", attributes={"payment.amount": float(total)}):
            charge(body.card_number, total)
    except PaymentDeclined as e:
        # Handled: the customer sees a message, Fixwire still gets the event,
        # with the order as context. The card number and email in the
        # message are masked on this machine before anything is sent.
        with fixwire.new_scope() as scope:
            scope.set_context("order", {"total": str(total), "items": len(cart)})
            scope.set_level("warning")
            fixwire.capture_exception(e)
        raise HTTPException(402, "the card was declined") from e
    # GatewayTimeout is not caught: the middleware reports it as unhandled.
    background.add_task(send_receipt, body.email, total)
    CARTS.pop(user, None)
    return {"status": "paid", "total": str(total)}


def charge(card_number: str, amount: Decimal) -> None:
    if card_number.endswith("0002"):
        raise PaymentDeclined("card %s declined for %s EUR" % (card_number, amount))
    if card_number.endswith("0009") or random.random() < float(os.environ.get("GATEWAY_FAILURE_RATE", "0")):
        raise GatewayTimeout("payment gateway timed out after 30s")


def send_receipt(email: str, total: Decimal) -> None:
    """Runs after the response; its failures are reported too."""
    if email.endswith("@bounce.example"):
        raise RuntimeError("receipt for %s bounced" % email)


@app.post("/admin/sync-inventory")
def sync_inventory() -> dict[str, str]:
    try:
        raise TimeoutError("warehouse API did not answer")
    except TimeoutError:
        # Logged errors become events (with the exception) by default.
        log.exception("inventory sync failed for %d products", len(PRODUCTS))
    return {"status": "partial"}
