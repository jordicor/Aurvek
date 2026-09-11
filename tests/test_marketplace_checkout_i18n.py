"""Real checkout handlers and SQLite schema; all provider calls are local fakes."""

import json
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from fastapi import HTTPException
from markupsafe import escape
from starlette.requests import Request

from billing import discounts
from i18n import LANGUAGES, Translator, get_translator
from marketplace import config
from marketplace.routes import checkout, packs
from marketplace.services.checkout_localization import discount_error_message
from marketplace.services.entitlements import grant_pack_entitlement, grant_prompt_entitlement


def request(payload=None, language="en", ios=False):
    async def receive():
        return {"type": "http.request", "body": json.dumps(payload or {}).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/checkout", "scheme": "https",
                    "server": ("fixture.test", 443), "query_string": b"",
                    "headers": [(b"accept-language", language.encode()), (b"x-aurvek-client", b"ios" if ios else b"web")]}, receive)


@pytest.fixture
def checkout_db(tmp_path, monkeypatch):
    path = tmp_path / "checkout.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript((Path(__file__).parents[1] / "aurvek_schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO USERS(id, username) VALUES(1, 'Creator <name>'),(2, 'Buyer')")
        conn.execute("INSERT INTO USER_DETAILS(user_id, balance) VALUES(1, 20),(2, 30)")
        conn.execute("INSERT INTO PROMPTS(id,name,description,public,purchase_price,created_by_user_id,public_id) VALUES(10,'Prompt <name>','Creator description',1,10,1,'public-prompt')")
        conn.execute("INSERT INTO PACKS(id,name,slug,description,is_public,is_paid,price,status,created_by_user_id,public_id) VALUES(20,'Pack <name>','test-pack','Pack description',1,1,10,'published',1,'public-pack')")
        conn.execute("INSERT INTO PACK_ITEMS(pack_id,prompt_id) VALUES(20,10)")
        conn.execute("INSERT INTO DISCOUNTS(code,discount_value,active,usage_count,unlimited_validity) VALUES('HALF',50,1,5,1),('FREE',100,1,5,1),('TINY',99,1,5,1),('EXPIRED',50,1,5,0)")
        conn.execute("UPDATE DISCOUNTS SET validity_date='2000-01-01' WHERE code='EXPIRED'")

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    for module in (checkout, packs, discounts):
        monkeypatch.setattr(module, "get_db_connection", connect)
    monkeypatch.setattr(config, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(checkout, "STRIPE_SECRET_KEY", "fixture-key")
    monkeypatch.setattr(packs, "STRIPE_SECRET_KEY", "fixture-key")
    monkeypatch.setattr(checkout, "upsert_creator_relationship", AsyncMock())
    # Pack code imports this normal helper locally; keep its unrelated writes scoped out.
    monkeypatch.setattr("common.upsert_creator_relationship", AsyncMock())
    async def context(req, user):
        get_translator(req, user)
        return {"request": req, "marketplace": {"enabled": True, "discovery_enabled": True}}
    monkeypatch.setattr(checkout, "get_template_context", context)
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create", Mock(side_effect=AssertionError("Unexpected provider call")))
    monkeypatch.setattr(checkout.stripe.checkout.Session, "retrieve", Mock(side_effect=AssertionError("Unexpected provider call")))
    return path, connect


def user(language):
    return SimpleNamespace(id=2, ui_language=language)


async def buy(kind, language, payload=None):
    route = checkout.api_purchase_prompt if kind == "prompt" else packs.api_purchase_pack
    return await route(10 if kind == "prompt" else 20, request(payload, language="de"), user(language))


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_checkout_locale_keeps_usd_amount_discount_reservation_and_creator_content(checkout_db, monkeypatch, language, kind):
    create = Mock(return_value=SimpleNamespace(url="https://checkout.example.test/session"))
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create", create)
    response = await buy(kind, language, {"discount_code": "HALF"})
    assert json.loads(response.body)["checkout_url"] == "https://checkout.example.test/session"
    args = create.call_args.kwargs
    assert args["locale"] == language
    assert args["line_items"][0]["price_data"]["currency"] == "usd"
    assert args["line_items"][0]["price_data"]["unit_amount"] == 500
    assert args["line_items"][0]["price_data"]["product_data"]["name"] == ("Prompt <name>" if kind == "prompt" else "Pack <name>")
    assert args["metadata"]["final_amount"] == "5.0"
    assert args["metadata"]["discount_claimed"] == "1"
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT usage_count FROM DISCOUNTS WHERE code='HALF'").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_checkout_known_discount_and_minimum_failures_are_localized_without_charging(checkout_db, language, kind):
    tr = Translator(language)
    for discount, key in [("EXPIRED", "discount_expired"), ("MISSING", "discount_invalid_code"), ("TINY", "minimum_amount")]:
        with pytest.raises(HTTPException) as caught:
            await buy(kind, language, {"discount_code": discount})
        assert caught.value.status_code == 400
        params = {"amount": tr.format_currency(10 * (1 - 99 / 100)), "minimum": tr.format_currency(.5)} if key == "minimum_amount" else {}
        assert caught.value.detail == tr.t("marketplace.checkout." + key, **params)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT usage_count FROM DISCOUNTS WHERE code='TINY'").fetchone()[0] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_failed_provider_restores_discount_and_returns_safe_localized_failure(checkout_db, monkeypatch, kind):
    error = checkout.stripe.error.StripeError("private provider details")
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create", Mock(side_effect=error))
    with pytest.raises(HTTPException) as caught:
        await buy(kind, "es", {"discount_code": "HALF"})
    assert caught.value.status_code == 500
    key = "processing_error" if kind == "prompt" else "service_error"
    assert caught.value.detail == Translator("es").t("marketplace.checkout." + key)
    assert "private" not in caught.value.detail
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT usage_count FROM DISCOUNTS WHERE code='HALF'").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_full_discount_keeps_atomic_grant_and_numeric_audit_values(checkout_db, kind):
    response = await buy(kind, "ja", {"discount_code": "FREE"})
    data = json.loads(response.body)
    assert data["free_purchase"] is True
    assert data["redirect"] == "/chat"
    key = "prompt_discount_granted" if kind == "prompt" else "pack_discount_claimed"
    assert data["message"] == Translator("ja").t("marketplace.checkout." + key)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT asset_type,user_id FROM ENTITLEMENTS").fetchone() == (kind, 2)
        assert conn.execute("SELECT usage_count FROM DISCOUNTS WHERE code='FREE'").fetchone()[0] == 4
        assert conn.execute("SELECT amount FROM TRANSACTIONS WHERE type=?", (kind + "_purchase",)).fetchone()[0] == 0
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30


@pytest.mark.asyncio
async def test_pack_config_unavailability_rolls_back_free_checkout(checkout_db, monkeypatch):
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE PACKS SET landing_reg_config=? WHERE id=20", (json.dumps({"billing_mode": "user_pays"}),))
    monkeypatch.setattr(packs, "get_balance", AsyncMock(return_value=0))
    with pytest.raises(HTTPException) as caught:
        await buy("pack", "de", {"discount_code": "FREE"})
    assert caught.value.status_code == 503
    assert caught.value.detail == Translator("de").t("marketplace.checkout.pack_unavailable")
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM PACK_PURCHASES").fetchone()[0] == 0
        assert conn.execute("SELECT usage_count FROM DISCOUNTS WHERE code='FREE'").fetchone()[0] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
async def test_free_pack_claim_and_repeat_preserve_access_and_locale(checkout_db, language):
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE PACKS SET is_paid=0,price=0 WHERE id=20")
    for key in ["pack_claimed", "pack_owned"]:
        response = await packs.api_claim_free_pack(20, request(language="en"), user(language))
        assert json.loads(response.body)["message"] == Translator(language).t("marketplace.checkout." + key)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS WHERE user_id=2").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_success_page_does_not_claim_payment_or_access_without_verified_state(checkout_db, monkeypatch, language, kind):
    route = checkout.prompt_purchase_success_page if kind == "prompt" else checkout.pack_purchase_success_page
    tr = Translator(language)
    for status, payment, expected in [("open", "unpaid", "pending"), ("expired", "unpaid", "not_completed"), ("complete", "paid", "confirmed")]:
        session = SimpleNamespace(status=status, payment_status=payment, metadata={
            "buyer_user_id": "2", "type": kind + "_purchase", kind + "_id": "10" if kind == "prompt" else "20", "final_amount": "10.00",
        })
        monkeypatch.setattr(checkout.stripe.checkout.Session, "retrieve", Mock(return_value=session))
        response = await route(request(language="en"), "fixture-session", user(language))
        html = response.body.decode()
        assert f'<html lang="{language}"' in html
        assert str(escape(tr.t("marketplace.purchase." + expected))) in html
        assert '<div class="access-badge">' not in html
        assert (str(escape(tr.format_currency(10))) in html) == (payment == "paid")
    session.metadata["buyer_user_id"] = "999"
    response = await route(request(), "fixture-session", user(language))
    assert str(escape(tr.t("marketplace.purchase.unverified"))) in response.body.decode()
    assert "Creator &lt;name&gt;" not in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
async def test_verified_paid_and_provisioned_success_page_uses_existing_entitlement(checkout_db, monkeypatch, kind):
    session = SimpleNamespace(status="complete", payment_status="paid", metadata={
        "buyer_user_id": "2", "type": kind + "_purchase", kind + "_id": "10" if kind == "prompt" else "20", "final_amount": "10.00",
    })
    monkeypatch.setattr(checkout.stripe.checkout.Session, "retrieve", Mock(return_value=session))
    async with checkout_db[1]() as conn:
        if kind == "prompt":
            await grant_prompt_entitlement(conn, user_id=2, prompt_id=10, source="fixture")
        else:
            await grant_pack_entitlement(conn, user_id=2, pack_id=20, source="fixture")
        await conn.commit()
    route = checkout.prompt_purchase_success_page if kind == "prompt" else checkout.pack_purchase_success_page
    response = await route(request(), "fixture-session", user("pt"))
    html = response.body.decode()
    assert '<div class="access-badge">' in html
    assert str(escape(Translator("pt").t("marketplace.purchase.success"))) in html
    assert "Creator &lt;name&gt;" in html


def test_unrecognized_discount_exception_has_no_technical_detail_in_any_language():
    error = discounts.DiscountError("private provider exception", 409)
    assert error.message == "private provider exception"
    assert error.status_code == 409
    for language in LANGUAGES:
        tr = Translator(language)
        assert discount_error_message(error, tr) == tr.t("marketplace.checkout.discount_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
async def test_checkout_auth_feature_gate_and_ios_contracts_are_localized(monkeypatch, language):
    tr = Translator(language)
    monkeypatch.setattr(config, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.delenv("IOS_PURCHASES_ENABLED", raising=False)
    for route, product_id in [(checkout.api_purchase_prompt, 10), (packs.api_purchase_pack, 20), (packs.api_claim_free_pack, 20)]:
        with pytest.raises(HTTPException) as caught:
            await route(product_id, request(language=language), None)
        assert caught.value.status_code == 401
        assert caught.value.detail == tr.t("marketplace.checkout.not_authenticated")
    for route, product_id in [(checkout.api_purchase_prompt, 10), (packs.api_purchase_pack, 20)]:
        response = await route(product_id, request(ios=True), user(language))
        data = json.loads(response.body)
        assert response.status_code == 409
        assert data["error"] == "ios_purchases_disabled"
        assert data["purchase_available"] is False
        assert data["message"] == tr.t("marketplace.checkout.ios_unavailable")
    monkeypatch.setattr(config, "marketplace_checkout_enabled", lambda: False)
    with pytest.raises(HTTPException) as caught:
        await checkout.prompt_purchase_bridge(request(language=language), 10, None)
    assert caught.value.status_code == 404
    assert caught.value.detail == tr.t("marketplace.checkout.not_found")
