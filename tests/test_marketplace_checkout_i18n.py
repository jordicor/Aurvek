"""Real checkout handlers and SQLite schema; all provider calls are local fakes."""

import json
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import common
import pytest
from fastapi import HTTPException
from markupsafe import escape
from starlette.requests import Request

from billing import discounts, usage_reservations
from i18n import LANGUAGES, Translator, get_translator
from marketplace import config
from marketplace.payments import checkout_webhooks
from marketplace.routes import checkout, packs
from marketplace.services.checkout_localization import discount_error_message
from marketplace.services.entitlements import grant_pack_entitlement, grant_prompt_entitlement, user_has_pack_access


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

    for module in (checkout, packs, discounts, checkout_webhooks, common, usage_reservations):
        monkeypatch.setattr(module, "get_db_connection", connect)
    monkeypatch.setattr(config, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(checkout_webhooks, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(common, "_pricing_config_cache", {})
    monkeypatch.setattr(common, "_pricing_config_cache_time", 0)
    monkeypatch.setattr(checkout, "STRIPE_SECRET_KEY", "fixture-key")
    monkeypatch.setattr(packs, "STRIPE_SECRET_KEY", "fixture-key")
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
    set_landing_config(checkout_db[0], kind, paid_landing_config())
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
        assert conn.execute("SELECT billing_account_id,billing_limit,billing_limit_action,billing_auto_refill_amount,billing_max_limit FROM USER_DETAILS WHERE user_id=2").fetchone() == (1, .5, "block", .75, 2)
        assert conn.execute("SELECT COUNT(*) FROM CREATOR_EARNINGS").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM USER_CREATOR_RELATIONSHIPS").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("billing_account_id", [None, 1])
async def test_pack_config_unavailability_rolls_back_free_checkout(checkout_db, billing_account_id):
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE PACKS SET landing_reg_config=? WHERE id=20", (json.dumps({"billing_mode": "user_pays"}),))
        conn.execute("UPDATE USER_DETAILS SET balance=0 WHERE user_id=1")
        conn.execute("UPDATE USER_DETAILS SET billing_account_id=? WHERE user_id=2", (billing_account_id,))
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


def paid_landing_config(initial_balance=4):
    return {
        "initial_balance": initial_balance,
        "billing_mode": "user_pays",
        "billing_limit": .5,
        "billing_limit_action": "block",
        "billing_auto_refill_amount": .75,
        "billing_max_limit": 2,
        "public_prompts_access": False,
        "allow_file_upload": True,
        "allow_image_generation": True,
    }


def set_landing_config(db_path, kind, config):
    column = "landing_registration_config" if kind == "prompt" else "landing_reg_config"
    product_id = 10 if kind == "prompt" else 20
    raw = config if isinstance(config, str) else json.dumps(config)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"UPDATE {kind.upper()}S SET {column}=? WHERE id=?", (raw, product_id))


def purchase_metadata(kind, **overrides):
    return {"type": kind + "_purchase", kind + "_id": "10" if kind == "prompt" else "20",
            "buyer_user_id": "2", "final_amount": "5", "discount_code": "HALF", "discount_value": "50",
            **overrides}


async def complete_purchase(kind, session_id=None, **overrides):
    return await checkout_webhooks.handle_marketplace_checkout_completed(
        SimpleNamespace(id=session_id or f"checkout-{kind}"), purchase_metadata(kind, **overrides),
    )


async def refund_purchase(kind, session_id=None, **overrides):
    return await checkout_webhooks.handle_marketplace_chargeback(
        SimpleNamespace(id=session_id or f"checkout-{kind}"), purchase_metadata(kind, **overrides), "fixture-intent",
    )


@pytest.mark.asyncio
async def test_free_prompt_with_unfunded_creator_preserves_its_current_billing_behavior(checkout_db):
    set_landing_config(checkout_db[0], "prompt", paid_landing_config())
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE USER_DETAILS SET balance=0 WHERE user_id=1")
    response = await buy("prompt", "en", {"discount_code": "FREE"})
    assert json.loads(response.body)["free_purchase"] is True
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance,billing_account_id FROM USER_DETAILS WHERE user_id=2").fetchone() == (30, None)
        assert conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS WHERE status='active'").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
@pytest.mark.parametrize("initial_balance,credit,creator_net", [(4, 2, 1.5), (20, 3.5, 0)])
async def test_paid_purchase_applies_complete_policy_and_credits_only_once(checkout_db, kind, initial_balance, credit, creator_net):
    set_landing_config(checkout_db[0], kind, paid_landing_config(initial_balance))
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE USER_DETAILS SET balance=0 WHERE user_id=1")
        conn.execute("UPDATE USER_DETAILS SET public_prompts_access=1 WHERE user_id=2")

    for _ in range(2):
        assert await complete_purchase(kind) == {"status": "success"}

    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT billing_account_id,billing_limit,billing_limit_action,billing_auto_refill_amount,billing_max_limit FROM USER_DETAILS WHERE user_id=2").fetchone() == (1, .5, "block", .75, 2)
        assert conn.execute("SELECT balance,current_prompt_id,public_prompts_access,allow_file_upload,allow_image_generation FROM USER_DETAILS WHERE user_id=2").fetchone() == (30 + credit, 10, 1, 1, 1)
        assert conn.execute(f"SELECT buyer_user_id,amount,status,payment_reference FROM {kind.upper()}_PURCHASES").fetchall() == [(2, 5, "completed", f"checkout-{kind}")]
        assert conn.execute("SELECT asset_type,asset_id,status,source_ref_id FROM ENTITLEMENTS").fetchall() == [(kind, 10 if kind == "prompt" else 20, "active", f"checkout-{kind}")]
        assert conn.execute("SELECT type,amount,balance_before,balance_after,discount_code FROM TRANSACTIONS ORDER BY id").fetchall() == [
            (kind + "_purchase", 5, 30, 30, "HALF"), ("balance_credit", credit, 30, 30 + credit, None),
        ]
        assert conn.execute("SELECT balance,pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone() == (0, creator_net)
        earnings = conn.execute("SELECT gross_amount,platform_commission,net_earnings,earning_type,source_ref,pack_id FROM CREATOR_EARNINGS").fetchall()
        assert earnings == ([(5, 1.5, creator_net, kind + "_purchase", f"checkout-{kind}", 20 if kind == "pack" else None)] if creator_net else [])
        assert conn.execute("SELECT source_type,source_id FROM USER_CREATOR_RELATIONSHIPS").fetchall() == [(kind, 10 if kind == "prompt" else 20)]

    with pytest.raises(usage_reservations.BillingLimitExceededError):
        await usage_reservations.reserve_ai_usage(user_id=2, maximum_amount=.6)
    with pytest.raises(usage_reservations.InsufficientBalanceError):
        await usage_reservations.reserve_ai_usage(user_id=2, maximum_amount=.25)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM BILLING_USAGE_RESERVATIONS").fetchone()[0] == 0
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30 + credit


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
@pytest.mark.parametrize("payer", [1, 3])
async def test_paid_purchase_preserves_existing_payer_and_limits(checkout_db, kind, payer):
    set_landing_config(checkout_db[0], kind, paid_landing_config())
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("INSERT INTO USERS(id,username) VALUES(3,'Existing payer')")
        conn.execute("INSERT INTO USER_DETAILS(user_id,balance) VALUES(3,40)")
        conn.execute("UPDATE USER_DETAILS SET balance=0 WHERE user_id=1")
        conn.execute("UPDATE USER_DETAILS SET billing_account_id=?,billing_limit=3,billing_limit_action='auto_refill',billing_auto_refill_amount=2,billing_max_limit=8,billing_current_month_spent=1 WHERE user_id=2", (payer,))

    assert await complete_purchase(kind) == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT billing_account_id,billing_limit,billing_limit_action,billing_auto_refill_amount,billing_max_limit,billing_current_month_spent FROM USER_DETAILS WHERE user_id=2").fetchone() == (payer, 3, "auto_refill", 2, 8, 1)
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
@pytest.mark.parametrize("failure", ["invalid_json", "update_failure", "missing_user_details"])
async def test_paid_config_failure_rolls_back_and_corrected_retry_fulfills_once(checkout_db, kind, failure):
    set_landing_config(checkout_db[0], kind, "{" if failure == "invalid_json" else paid_landing_config())
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        if failure == "update_failure":
            conn.execute("""
                CREATE TRIGGER fail_purchase_config
                BEFORE UPDATE OF balance, billing_account_id ON USER_DETAILS
                WHEN OLD.user_id = 2
                BEGIN
                    SELECT RAISE(ABORT, 'Configuration update failed');
                END
            """)
        elif failure == "missing_user_details":
            conn.execute("DELETE FROM USER_DETAILS WHERE user_id=2")
        before = conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall()

    with pytest.raises(HTTPException) as caught:
        await complete_purchase(kind)
    assert caught.value.status_code == 500
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall() == before
        for table in (kind.upper() + "_PURCHASES", "ENTITLEMENTS", "TRANSACTIONS", "CREATOR_EARNINGS", "USER_CREATOR_RELATIONSHIPS"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        if failure == "update_failure":
            conn.execute("DROP TRIGGER fail_purchase_config")
        elif failure == "missing_user_details":
            conn.execute("INSERT INTO USER_DETAILS(user_id,balance) VALUES(2,30)")
    set_landing_config(checkout_db[0], kind, paid_landing_config())
    for _ in range(2):
        assert await complete_purchase(kind) == {"status": "success"}

    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance,billing_account_id,billing_limit,billing_limit_action,billing_auto_refill_amount,billing_max_limit FROM USER_DETAILS WHERE user_id=2").fetchone() == (32, 1, .5, "block", .75, 2)
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 1.5
        assert conn.execute("SELECT type,amount FROM TRANSACTIONS ORDER BY id").fetchall() == [(kind + "_purchase", 5), ("balance_credit", 2)]
        assert conn.execute("SELECT net_earnings,source_ref FROM CREATOR_EARNINGS").fetchall() == [(1.5, f"checkout-{kind}")]
        assert conn.execute(f"SELECT status FROM {kind.upper()}_PURCHASES").fetchall() == [("completed",)]
        assert conn.execute("SELECT status FROM ENTITLEMENTS").fetchall() == [("active",)]
        assert conn.execute("SELECT COUNT(*) FROM USER_CREATOR_RELATIONSHIPS").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
@pytest.mark.parametrize("initial_balance,amount,discount,credit,creator_net", [(10, 10, 0, 7, 0), (4, 5, 50, 2, 1.5)])
async def test_chargeback_uses_sale_evidence_after_config_owner_and_commission_change(checkout_db, kind, initial_balance, amount, discount, credit, creator_net):
    set_landing_config(checkout_db[0], kind, paid_landing_config(initial_balance))
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE USER_DETAILS SET pending_earnings=70 WHERE user_id=1")
        conn.execute("INSERT INTO USERS(id,username) VALUES(3,'Later owner')")
        conn.execute("INSERT INTO USER_DETAILS(user_id,balance,pending_earnings) VALUES(3,40,45)")
    assert await complete_purchase(kind, final_amount=str(amount), discount_value=str(discount), discount_code="HALF" if discount else "") == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        # Legacy sales with both positive ledger records remain reversible.
        if creator_net:
            conn.execute("UPDATE ENTITLEMENTS SET metadata_json=json_remove(metadata_json,'$.purchase_fulfillment')")
        prior_metadata = json.loads(conn.execute("SELECT metadata_json FROM ENTITLEMENTS").fetchone()[0])
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30 + credit

    set_landing_config(checkout_db[0], kind, {"initial_balance": 100, "billing_mode": "customer_pays"})
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute(f"UPDATE {kind.upper()}S SET created_by_user_id=3")
        conn.execute("INSERT INTO SYSTEM_CONFIG(key,value) VALUES('pricing_commission','90')")
    common._pricing_config_cache.clear()
    common._pricing_config_cache_time = 0

    assert await refund_purchase(kind, final_amount=str(amount), discount_value=str(discount)) == {"status": "success"}
    assert await refund_purchase(kind, final_amount=str(amount), discount_value=str(discount)) == {"status": "already_processed"}
    assert await complete_purchase(kind, final_amount=str(amount), discount_value=str(discount)) == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 70
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=3").fetchone()[0] == 45
        assert conn.execute(f"SELECT status FROM {kind.upper()}_PURCHASES").fetchall() == [("refunded",)]
        assert conn.execute("SELECT status FROM ENTITLEMENTS").fetchall() == [("refunded",)]
        after_metadata = json.loads(conn.execute("SELECT metadata_json FROM ENTITLEMENTS").fetchone()[0])
        assert all(after_metadata[key] == value for key, value in prior_metadata.items())
        assert conn.execute("SELECT amount,balance_before,balance_after FROM TRANSACTIONS WHERE type='chargeback_reversal'").fetchall() == [(credit, 30 + credit, 30)]
        assert conn.execute("SELECT COALESCE(SUM(net_earnings),0) FROM CREATOR_EARNINGS").fetchone()[0] == 0
        assert conn.execute("SELECT net_earnings FROM CREATOR_EARNINGS ORDER BY id").fetchall() == ([(creator_net,), (-creator_net,)] if creator_net else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prompt", "pack"])
@pytest.mark.parametrize("missing", ["credit", "earnings"])
async def test_historical_chargeback_requires_evidence_before_mutating_access_or_money(checkout_db, kind, missing):
    set_landing_config(checkout_db[0], kind, paid_landing_config())
    assert await complete_purchase(kind) == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE ENTITLEMENTS SET metadata_json=NULL")
        if missing == "credit":
            conn.execute("DELETE FROM TRANSACTIONS WHERE type='balance_credit'")
        else:
            conn.execute("DELETE FROM CREATOR_EARNINGS")
        before = conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall()
    with pytest.raises(HTTPException) as caught:
        await refund_purchase(kind)
    assert caught.value.status_code == 409
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall() == before
        assert conn.execute(f"SELECT status FROM {kind.upper()}_PURCHASES").fetchall() == [("completed",)]
        assert conn.execute("SELECT status FROM ENTITLEMENTS").fetchall() == [("active",)]
        assert conn.execute("SELECT COUNT(*) FROM TRANSACTIONS WHERE type='chargeback_reversal'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_pack_refund_with_no_active_prompt_uses_recorded_pending_credit(checkout_db):
    set_landing_config(checkout_db[0], "pack", {"initial_balance": 4})
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.execute("UPDATE PACK_ITEMS SET is_active=0")
    assert await complete_purchase("pack") == {"status": "success"}
    set_landing_config(checkout_db[0], "pack", {"initial_balance": 0})
    assert await refund_purchase("pack") == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM CREATOR_EARNINGS").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM ENTITLEMENTS").fetchone() == ("refunded",)


@pytest.mark.asyncio
async def test_chargeback_records_creator_debt_after_payout_and_only_debits_available_buyer_funds(checkout_db):
    set_landing_config(checkout_db[0], "prompt", paid_landing_config())
    assert await complete_purchase("prompt") == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        # Scoped prerequisites: earnings already transferred; buyer spent funds.
        conn.execute("UPDATE USER_DETAILS SET pending_earnings=0 WHERE user_id=1")
        conn.execute("INSERT INTO TRANSACTIONS(user_id,type,amount,balance_before,balance_after,reference_id) VALUES(1,'payout_completed',1.5,0,0,'fixture-transfer')")
        conn.execute("UPDATE USER_DETAILS SET balance=1 WHERE user_id=2")
    assert await refund_purchase("prompt") == {"status": "success"}
    assert await refund_purchase("prompt") == {"status": "already_processed"}
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 0
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == -1.5
        assert conn.execute("SELECT amount,balance_before,balance_after FROM TRANSACTIONS WHERE type='chargeback_reversal'").fetchall() == [(1, 1, 0)]
        assert conn.execute("SELECT COALESCE(SUM(net_earnings),0) FROM CREATOR_EARNINGS").fetchone()[0] == 0
        assert conn.execute("SELECT amount FROM TRANSACTIONS WHERE type='payout_completed'").fetchall() == [(1.5,)]


BILLING_POLICY_COLUMNS = (
    "billing_account_id,billing_acquisition_ref,billing_limit,billing_limit_action,"
    "billing_auto_refill_amount,billing_max_limit"
)


def billing_policy(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(f"SELECT {BILLING_POLICY_COLUMNS} FROM USER_DETAILS WHERE user_id=2").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_balance,credit,creator_net", [(0, 0, 3.5), (4, 2, 1.5)])
async def test_pack_chargeback_after_admin_revocation_preserves_evidence_and_removes_unsupported_billing(checkout_db, initial_balance, credit, creator_net):
    from tests.test_user_management_security import DummyUser

    set_landing_config(checkout_db[0], "pack", paid_landing_config(initial_balance))
    assert await complete_purchase("pack") == {"status": "success"}
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        original_metadata = json.loads(conn.execute("SELECT metadata_json FROM ENTITLEMENTS").fetchone()[0])
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30 + credit
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == creator_net

    response = await packs.api_revoke_pack_access(2, 20, DummyUser(1, "Admin", admin=True))
    assert response.status_code == 200
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT status,revoked_by_user_id FROM ENTITLEMENTS").fetchone() == ("revoked", 1)
        revoked_at = conn.execute("SELECT revoked_at FROM ENTITLEMENTS").fetchone()[0]
    async with checkout_db[1]() as conn:
        assert not await user_has_pack_access(conn, user_id=2, pack_id=20)

    assert await refund_purchase("pack") == {"status": "success"}
    assert await refund_purchase("pack") == {"status": "already_processed"}
    assert await complete_purchase("pack") == {"status": "success"}
    assert billing_policy(checkout_db[0]) == (None, None, None, "block", 10, None)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT status FROM PACK_PURCHASES").fetchone() == ("refunded",)
        assert conn.execute("SELECT status,revoked_by_user_id,revoked_at FROM ENTITLEMENTS").fetchone() == ("refunded", 1, revoked_at)
        final_metadata = json.loads(conn.execute("SELECT metadata_json FROM ENTITLEMENTS").fetchone()[0])
        assert all(final_metadata[key] == value for key, value in original_metadata.items())
        assert final_metadata["route"] == "api_revoke_pack_access"
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 0
        assert conn.execute("SELECT amount,balance_before,balance_after FROM TRANSACTIONS WHERE type='chargeback_reversal'").fetchall() == [(credit, 30 + credit, 30)]
        assert conn.execute("SELECT net_earnings FROM CREATOR_EARNINGS ORDER BY id").fetchall() == [(creator_net,), (-creator_net,)]
    async with checkout_db[1]() as conn:
        assert not await user_has_pack_access(conn, user_id=2, pack_id=20)


@pytest.mark.asyncio
@pytest.mark.parametrize("refund_order,supports_billing,revoke_origin", [
    (("pack", "prompt"), True, False), (("prompt", "pack"), True, False),
    (("pack", "prompt"), False, False), (("pack", "prompt"), True, True),
])
async def test_refund_preserves_origin_policy_until_last_supporting_purchase_is_returned(checkout_db, refund_order, supports_billing, revoke_origin):
    set_landing_config(checkout_db[0], "pack", paid_landing_config(initial_balance=0))
    set_landing_config(checkout_db[0], "prompt", {
        **paid_landing_config(initial_balance=0), "billing_limit": 9,
        "billing_mode": "user_pays" if supports_billing else "customer_pays",
        "billing_limit_action": "notify", "billing_auto_refill_amount": 3, "billing_max_limit": 50,
    })
    assert await complete_purchase("pack", "origin-A") == {"status": "success"}
    assert await complete_purchase("prompt", "support-B") == {"status": "success"}
    origin_policy = (1, "origin-A", .5, "block", .75, 2)
    assert billing_policy(checkout_db[0]) == origin_policy

    if revoke_origin:
        from tests.test_user_management_security import DummyUser

        response = await packs.api_revoke_pack_access(2, 20, DummyUser(1, "Admin", admin=True))
        assert response.status_code == 200

    sessions = {"pack": "origin-A", "prompt": "support-B"}
    first, last = refund_order
    assert await refund_purchase(first, sessions[first]) == {"status": "success"}
    assert await refund_purchase(first, sessions[first]) == {"status": "already_processed"}
    expected_policy = origin_policy if supports_billing else (None, None, None, "block", 10, None)
    assert billing_policy(checkout_db[0]) == expected_policy
    assert await refund_purchase(last, sessions[last]) == {"status": "success"}
    assert await refund_purchase(last, sessions[last]) == {"status": "already_processed"}
    assert billing_policy(checkout_db[0]) == (None, None, None, "block", 10, None)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT status FROM ENTITLEMENTS ORDER BY id").fetchall() == [("refunded",), ("refunded",)]
        assert conn.execute("SELECT pending_earnings FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", ["assignment", "active_support"])
async def test_refund_keeps_billing_when_historical_provenance_is_ambiguous(checkout_db, historical):
    set_landing_config(checkout_db[0], "pack", paid_landing_config())
    assert await complete_purchase("pack") == {"status": "success"}
    if historical == "assignment":
        with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
            conn.execute("UPDATE USER_DETAILS SET billing_acquisition_ref=NULL WHERE user_id=2")
    else:
        async with checkout_db[1]() as conn:
            await grant_prompt_entitlement(conn, user_id=2, prompt_id=10, source="historical")
            await conn.commit()
    prior_policy = billing_policy(checkout_db[0])
    assert await refund_purchase("pack") == {"status": "success"}
    assert await refund_purchase("pack") == {"status": "already_processed"}
    assert billing_policy(checkout_db[0]) == prior_policy
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30
        assert conn.execute("SELECT status FROM ENTITLEMENTS WHERE asset_type='pack'").fetchone() == ("refunded",)


@pytest.mark.asyncio
@pytest.mark.parametrize("administrator_id", [1, 3])
async def test_refund_preserves_subsequent_administrative_payer_or_limit_changes(checkout_db, monkeypatch, administrator_id):
    from tests.test_user_management_security import DummyUser, _update_kwargs, app_module

    monkeypatch.setattr(app_module, "get_db_connection", checkout_db[1])
    with closing(sqlite3.connect(checkout_db[0])) as conn, conn:
        conn.executemany("INSERT INTO USER_ROLES(id,role_name) VALUES(?,?)", [(1, "admin"), (3, "customer")])
        conn.execute("INSERT INTO LLM(id,machine,model) VALUES(1,'GPT','fixture-model')")
        conn.execute("UPDATE USERS SET role_id=1 WHERE id=1")
        conn.execute("UPDATE USERS SET role_id=3 WHERE id=2")
        conn.execute("INSERT INTO USERS(id,username,role_id) VALUES(3,'Later admin',1)")
        conn.execute("INSERT INTO USER_DETAILS(user_id,balance) VALUES(3,40)")
    set_landing_config(checkout_db[0], "prompt", paid_landing_config())
    assert await complete_purchase("prompt") == {"status": "success"}

    kwargs = _update_kwargs("Buyer", 32)
    kwargs.update(current_user=DummyUser(administrator_id, "Admin", admin=True), billing_mode="user_pays",
                  billing_limit=".5", billing_limit_action="block", billing_auto_refill_amount=".75", billing_max_limit="2")
    if administrator_id == 1:
        response = await app_module.update_user(**kwargs)
        assert response.status_code == 200 and json.loads(response.body)["success"] is True
        assert billing_policy(checkout_db[0]) == (1, "checkout-prompt", .5, "block", .75, 2)
    kwargs.update(billing_limit="4", billing_limit_action="notify", billing_auto_refill_amount="2", billing_max_limit="9")
    response = await app_module.update_user(**kwargs)
    assert response.status_code == 200 and json.loads(response.body)["success"] is True
    managed_policy = (administrator_id, None, 4, "notify", 2, 9)
    assert billing_policy(checkout_db[0]) == managed_policy
    assert await refund_purchase("prompt") == {"status": "success"}
    assert await refund_purchase("prompt") == {"status": "already_processed"}
    assert billing_policy(checkout_db[0]) == managed_policy


@pytest.mark.asyncio
async def test_refund_rolls_back_while_sponsored_usage_is_active_and_can_be_retried(checkout_db):
    set_landing_config(checkout_db[0], "prompt", paid_landing_config())
    assert await complete_purchase("prompt") == {"status": "success"}
    reservation_id = await usage_reservations.reserve_ai_usage(user_id=2, maximum_amount=.25)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        prior_details = conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall()
    with pytest.raises(HTTPException) as caught:
        await refund_purchase("prompt")
    assert caught.value.status_code == 409
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT * FROM USER_DETAILS ORDER BY user_id").fetchall() == prior_details
        assert conn.execute("SELECT status FROM PROMPT_PURCHASES").fetchone() == ("completed",)
        assert conn.execute("SELECT status FROM ENTITLEMENTS").fetchone() == ("active",)
        assert conn.execute("SELECT COUNT(*) FROM CREATOR_EARNINGS").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM TRANSACTIONS WHERE type='chargeback_reversal'").fetchone()[0] == 0
    assert await usage_reservations.refund_fixed_usage(reservation_id) is True
    assert await refund_purchase("prompt") == {"status": "success"}
    assert billing_policy(checkout_db[0]) == (None, None, None, "block", 10, None)
    with closing(sqlite3.connect(checkout_db[0])) as conn:
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == 20
        assert conn.execute("SELECT balance FROM USER_DETAILS WHERE user_id=2").fetchone()[0] == 30
