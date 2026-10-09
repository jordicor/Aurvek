import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import Mock

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from starlette.templating import Jinja2Templates
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape
from i18n import Translator, template_context

from billing import discounts as discounts_module
from billing import wallet as wallet_module
from billing.discounts import (
    DiscountError,
    claim_discount_usage_for_checkout,
    validate_discount_code,
)
from billing.routes import discounts as discount_routes
from billing.routes import wallet as wallet_routes


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("status,paid,credit,expected", [
    ("expired", "unpaid", False, "not_completed"),
    ("open", "unpaid", False, "pending"),
    ("complete", "unpaid", False, "pending"),
    ("complete", "paid", False, "crediting"),
    ("complete", "paid", True, "credited"),
])
async def test_wallet_success_reports_payment_and_actual_credit_separately(wallet_db, monkeypatch, status, paid, credit, expected):
    session = wallet_module.stripe.checkout.Session.construct_from(
        {"id": "cs-wallet", "status": status, "payment_status": paid,
         "metadata": {"user_id": "1", "original_amount": "10"}}, None)
    monkeypatch.setattr(wallet_module, "STRIPE_SECRET_KEY", "fixture-key")
    monkeypatch.setattr(wallet_module.stripe.checkout.Session, "retrieve", Mock(return_value=session))
    with sqlite3.connect(wallet_db) as db:
        # Similar entries from other users, sessions or types do not confirm this top-up.
        db.executemany("""INSERT INTO TRANSACTIONS
            (user_id,type,amount,balance_before,balance_after,reference_id) VALUES (?,?,10,2,12,?)""",
            [(2, "payment", "cs-wallet"), (1, "payment", "cs-other"), (1, "balance_credit", "cs-wallet")])
        if credit:
            db.execute("""INSERT INTO TRANSACTIONS
                (user_id,type,amount,balance_before,balance_after,reference_id) VALUES (1,'payment',10,2,12,'cs-wallet')""")
            db.execute("UPDATE USER_DETAILS SET balance=12 WHERE user_id=1")
        entries = db.execute("SELECT * FROM TRANSACTIONS").fetchall()
    env = Environment(loader=ChoiceLoader([
        DictLoader({"base.html": "{% block title %}{% endblock %}{% block styles %}{% endblock %}{% block content %}{% endblock %}"}),
        FileSystemLoader("templates"),
    ]), autoescape=select_autoescape())
    monkeypatch.setattr(wallet_routes, "templates", Jinja2Templates(env=env))

    async def page_context(request, user):
        return {"request": request, **template_context(request)}

    monkeypatch.setattr(wallet_routes, "get_template_context", page_context)
    request = Request({"type": "http", "method": "GET", "path": "/payment-success",
                       "headers": [(b"host", b"testserver"), (b"accept-language", b"es")],
                       "query_string": b"session_id=cs-wallet", "scheme": "https"})
    response = await wallet_routes.payment_success_page(request, "cs-wallet", DummyUser(1))
    assert response.context["payment_state"] == expected
    assert response.context["new_balance"] == (12 if credit else None)
    html = response.body.decode()
    assert Translator("es").render(f"wallet.payment.{expected}.title") in html
    assert ('http-equiv="refresh"' in html) is (expected in {"pending", "crediting"})
    assert ('class="balance-amount"' in html) is credit
    if expected == "crediting":
        final = await wallet_routes.payment_success_page(request, "cs-wallet", DummyUser(1), check=4)
        assert 'http-equiv="refresh"' not in final.body.decode()
        assert Translator("es").render("wallet.payment.check_later") in final.body.decode()
    with sqlite3.connect(wallet_db) as db:
        assert db.execute("SELECT * FROM TRANSACTIONS").fetchall() == entries
        assert db.execute("SELECT balance FROM USER_DETAILS WHERE user_id=1").fetchone()[0] == (12 if credit else 2)

    # Even a paid session with an entry must never disclose another user's wallet.
    if credit:
        session["metadata"]["user_id"] = "2"
        denied = await wallet_routes.payment_success_page(request, "cs-wallet", DummyUser(1))
        assert denied.status_code == 302 and denied.headers["location"] == "/payment"


class DummyUser:
    def __init__(self, user_id: int):
        self.id = user_id


def _json_request(path: str, payload: dict) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


@pytest_asyncio.fixture
async def wallet_db(tmp_path, monkeypatch):
    db_path = tmp_path / "wallet.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL
        );
        CREATE TABLE USER_DETAILS (
            user_id INTEGER PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES USERS(id)
        );
        CREATE TABLE DISCOUNTS (
            code TEXT PRIMARY KEY,
            discount_value REAL,
            active INTEGER NOT NULL,
            validity_date TEXT,
            usage_count INTEGER,
            unlimited_usage INTEGER NOT NULL DEFAULT 0,
            unlimited_validity INTEGER NOT NULL DEFAULT 0,
            created_by_user_id INTEGER,
            scope TEXT NOT NULL DEFAULT 'marketplace'
                CHECK(scope IN ('marketplace', 'wallet')),
            wallet_grant_amount REAL
                CHECK(wallet_grant_amount IS NULL OR
                      (wallet_grant_amount >= 5 AND wallet_grant_amount <= 500))
        );
        CREATE TABLE DISCOUNT_REDEMPTIONS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discount_code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            grant_amount REAL NOT NULL,
            transaction_reference TEXT,
            redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(discount_code, user_id, purpose),
            FOREIGN KEY (user_id) REFERENCES USERS(id)
        );
        CREATE TABLE TRANSACTIONS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_before REAL NOT NULL,
            balance_after REAL NOT NULL,
            description TEXT,
            reference_id TEXT,
            discount_code TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO USERS (id, username) VALUES (1, 'user-one'), (2, 'user-two');
        INSERT INTO USER_DETAILS (user_id, balance) VALUES (1, 2), (2, 0);
        """
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def get_connection(readonly=False):
        mode = "ro" if readonly else "rw"
        db = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
        try:
            yield db
        finally:
            await db.close()

    monkeypatch.setattr(discounts_module, "get_db_connection", get_connection)
    monkeypatch.setattr(wallet_module, "get_db_connection", get_connection)
    monkeypatch.setattr(discount_routes, "get_db_connection", get_connection)
    return db_path


def _insert_code(
    db_path,
    code,
    *,
    scope,
    discount_value=0,
    grant_amount=None,
    usage_count=5,
    unlimited_usage=0,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO DISCOUNTS
            (code, discount_value, active, usage_count, unlimited_usage,
             unlimited_validity, scope, wallet_grant_amount)
        VALUES (?, ?, 1, ?, ?, 1, ?, ?)
        """,
        (
            code,
            discount_value,
            usage_count,
            unlimited_usage,
            scope,
            grant_amount,
        ),
    )
    conn.commit()
    conn.close()


async def test_free_credit_ignores_client_amount_and_uses_fixed_server_grant(wallet_db):
    _insert_code(wallet_db, "WALLET10", scope="wallet", grant_amount=10)
    response = await wallet_routes.free_credit_payment(
        _json_request(
            "/api/payment/free-credit",
            {
                "originalAmount": 500,
                "finalAmount": 0,
                "discount_code": "WALLET10",
            },
        ),
        current_user=DummyUser(1),
    )

    payload = json.loads(response.body)
    conn = sqlite3.connect(wallet_db)
    try:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 1"
        ).fetchone()[0]
        transaction = conn.execute(
            "SELECT amount, balance_before, balance_after FROM TRANSACTIONS"
        ).fetchone()
        redemption = conn.execute(
            "SELECT grant_amount, purpose FROM DISCOUNT_REDEMPTIONS"
        ).fetchone()
        remaining = conn.execute(
            "SELECT usage_count FROM DISCOUNTS WHERE code = 'WALLET10'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert payload["grant_amount"] == 10
    assert balance == 12
    assert transaction == (10, 2, 12)
    assert redemption == (10, "wallet_credit")
    assert remaining == 4


async def test_checkout_preview_uses_server_grant_without_crediting_client_amount(wallet_db):
    _insert_code(wallet_db, "CHECKOUT12", scope="wallet", grant_amount=12)

    result = await wallet_module.create_wallet_checkout(
        {"amount": 500, "discount_code": "CHECKOUT12"},
        "https://example.test",
        1,
    )

    assert result["free_purchase"] is True
    assert result["grant_amount"] == 12
    conn = sqlite3.connect(wallet_db)
    try:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 1"
        ).fetchone()[0]
        redemptions = conn.execute(
            "SELECT COUNT(*) FROM DISCOUNT_REDEMPTIONS"
        ).fetchone()[0]
    finally:
        conn.close()
    assert balance == 2
    assert redemptions == 0


async def test_wallet_credit_is_once_per_user_even_when_global_usage_is_unlimited(wallet_db):
    _insert_code(
        wallet_db,
        "UNLIMITED10",
        scope="wallet",
        grant_amount=10,
        usage_count=None,
        unlimited_usage=1,
    )
    await wallet_module.credit_free_wallet_topup(
        user_id=1,
        discount_code="UNLIMITED10",
        description_prefix="Wallet credit code",
        reference_prefix="test",
    )

    with pytest.raises(HTTPException) as reused:
        await wallet_module.credit_free_wallet_topup(
            user_id=1,
            discount_code="UNLIMITED10",
            description_prefix="Wallet credit code",
            reference_prefix="test",
        )
    assert reused.value.status_code == 409

    second_user = await wallet_module.credit_free_wallet_topup(
        user_id=2,
        discount_code="UNLIMITED10",
        description_prefix="Wallet credit code",
        reference_prefix="test",
    )
    assert second_user["new_balance"] == 10

    conn = sqlite3.connect(wallet_db)
    try:
        balances = conn.execute(
            "SELECT user_id, balance FROM USER_DETAILS ORDER BY user_id"
        ).fetchall()
        redemptions = conn.execute(
            "SELECT COUNT(*) FROM DISCOUNT_REDEMPTIONS"
        ).fetchone()[0]
    finally:
        conn.close()
    assert balances == [(1, 12), (2, 10)]
    assert redemptions == 2


async def test_concurrent_wallet_redemption_credits_only_once(wallet_db):
    _insert_code(
        wallet_db,
        "RACE10",
        scope="wallet",
        grant_amount=10,
        usage_count=2,
    )

    async def redeem():
        return await wallet_module.credit_free_wallet_topup(
            user_id=1,
            discount_code="RACE10",
            description_prefix="Wallet credit code",
            reference_prefix="race",
        )

    results = await asyncio.gather(redeem(), redeem(), return_exceptions=True)
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, HTTPException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409
    conn = sqlite3.connect(wallet_db)
    try:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 1"
        ).fetchone()[0]
        redemptions = conn.execute(
            "SELECT COUNT(*) FROM DISCOUNT_REDEMPTIONS"
        ).fetchone()[0]
        remaining = conn.execute(
            "SELECT usage_count FROM DISCOUNTS WHERE code = 'RACE10'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert balance == 12
    assert redemptions == 1
    assert remaining == 1


async def test_discount_scope_separates_marketplace_and_wallet(wallet_db):
    _insert_code(
        wallet_db,
        "MARKET100",
        scope="marketplace",
        discount_value=100,
        usage_count=2,
    )
    _insert_code(wallet_db, "WALLET15", scope="wallet", grant_amount=15)

    marketplace = await validate_discount_code("MARKET100", 50)
    assert marketplace.final_amount == 0
    with pytest.raises(DiscountError):
        await validate_discount_code("WALLET15", 50)

    with pytest.raises(HTTPException) as wrong_scope:
        await wallet_module.credit_free_wallet_topup(
            user_id=1,
            discount_code="MARKET100",
            description_prefix="Wallet credit code",
            reference_prefix="test",
        )
    assert wrong_scope.value.status_code == 400

    # Marketplace usage continues to work and only decrements marketplace codes.
    await claim_discount_usage_for_checkout("MARKET100", 20)
    conn = sqlite3.connect(wallet_db)
    try:
        marketplace_uses = conn.execute(
            "SELECT usage_count FROM DISCOUNTS WHERE code = 'MARKET100'"
        ).fetchone()[0]
        wallet_uses = conn.execute(
            "SELECT usage_count FROM DISCOUNTS WHERE code = 'WALLET15'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert marketplace_uses == 1
    assert wallet_uses == 5


async def test_wallet_preview_does_not_echo_or_use_manipulated_amount(wallet_db):
    _insert_code(wallet_db, "WALLET25", scope="wallet", grant_amount=25)

    response = await discount_routes.apply_discount(
        discount_code="WALLET25",
        amount=500,
        current_user=DummyUser(1),
    )
    payload = json.loads(response.body)

    assert payload == {
        "success": True,
        "scope": "wallet",
        "walletGrantAmount": 25,
        "newPrice": 0,
    }


async def test_admin_wallet_grant_validation_enforces_fixed_bounds(wallet_db):
    scope, percentage, grant = discount_routes._discount_values_for_scope(
        "wallet",
        100,
        5,
    )
    assert (scope, percentage, grant) == ("wallet", 0.0, 5.0)

    for invalid_grant in (4.99, 500.01, float("nan")):
        with pytest.raises(HTTPException) as exc_info:
            discount_routes._discount_values_for_scope(
                "wallet",
                100,
                invalid_grant,
            )
        assert exc_info.value.status_code == 400


async def test_wallet_credit_rolls_back_redemption_balance_and_usage_together(wallet_db):
    _insert_code(
        wallet_db,
        "ROLLBACK10",
        scope="wallet",
        grant_amount=10,
        usage_count=1,
    )
    conn = sqlite3.connect(wallet_db)
    conn.execute(
        """
        CREATE TRIGGER fail_wallet_transaction
        BEFORE INSERT ON TRANSACTIONS
        BEGIN
            SELECT RAISE(ABORT, 'forced transaction failure');
        END;
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(HTTPException) as failure:
        await wallet_module.credit_free_wallet_topup(
            user_id=1,
            discount_code="ROLLBACK10",
            description_prefix="Wallet credit code",
            reference_prefix="test",
        )
    assert failure.value.status_code == 500

    conn = sqlite3.connect(wallet_db)
    try:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 1"
        ).fetchone()[0]
        uses = conn.execute(
            "SELECT usage_count FROM DISCOUNTS WHERE code = 'ROLLBACK10'"
        ).fetchone()[0]
        redemptions = conn.execute(
            "SELECT COUNT(*) FROM DISCOUNT_REDEMPTIONS"
        ).fetchone()[0]
    finally:
        conn.close()

    assert balance == 2
    assert uses == 1
    assert redemptions == 0
