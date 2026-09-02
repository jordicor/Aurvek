import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest

import common


SCHEMA = """
CREATE TABLE USER_DETAILS (
    user_id INTEGER PRIMARY KEY,
    balance REAL NOT NULL,
    total_cost REAL NOT NULL DEFAULT 0,
    total_tts_cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE SERVICE_USAGE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    usage_quantity REAL NOT NULL,
    cost REAL NOT NULL
);
CREATE TABLE USAGE_DAILY (
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    operations INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    units REAL NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    updated_at TEXT,
    UNIQUE(user_id, date, type)
);
"""


@pytest.fixture()
def billing_db(tmp_path, monkeypatch):
    path = tmp_path / "tts-billing.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (1, 1.0)"
        )
        conn.commit()

    @asynccontextmanager
    async def get_connection(readonly=False):
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(common, "get_db_connection", get_connection)
    monkeypatch.setattr(
        common.Cost,
        "TTS_PROVIDER_SERVICES",
        {
            "elevenlabs": {"cost_per_character": 0.01, "service_id": 11},
            "openai": {"cost_per_character": 0.002, "service_id": 22},
        },
    )
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "service_id", "expected_cost"),
    [("elevenlabs", 11, 0.10), ("openai", 22, 0.02)],
)
async def test_tts_charge_and_refund_use_provider_service_and_rate(
    billing_db, provider, service_id, expected_cost
):
    assert await common.cost_tts(1, 10, provider=provider) is True

    with sqlite3.connect(billing_db) as conn:
        balance, total_cost, total_tts = conn.execute(
            "SELECT balance, total_cost, total_tts_cost FROM USER_DETAILS WHERE user_id=1"
        ).fetchone()
        charge = conn.execute(
            "SELECT service_id, usage_quantity, cost FROM SERVICE_USAGE ORDER BY id"
        ).fetchone()
        daily_charge = conn.execute(
            "SELECT operations, units, total_cost FROM USAGE_DAILY "
            "WHERE user_id=1 AND type='tts'"
        ).fetchone()

    assert balance == pytest.approx(1.0 - expected_cost)
    assert total_cost == pytest.approx(expected_cost)
    assert total_tts == pytest.approx(expected_cost)
    assert charge == pytest.approx((service_id, 10, expected_cost))
    assert daily_charge == pytest.approx((1, 10, expected_cost))

    assert await common.refund_tts(1, 10, provider=provider) is True

    with sqlite3.connect(billing_db) as conn:
        balance, total_cost, total_tts = conn.execute(
            "SELECT balance, total_cost, total_tts_cost FROM USER_DETAILS WHERE user_id=1"
        ).fetchone()
        refund = conn.execute(
            "SELECT service_id, usage_quantity, cost FROM SERVICE_USAGE ORDER BY id DESC"
        ).fetchone()
        daily_refund = conn.execute(
            "SELECT operations, units, total_cost FROM USAGE_DAILY "
            "WHERE user_id=1 AND type='tts'"
        ).fetchone()

    assert balance == pytest.approx(1.0)
    assert total_cost == pytest.approx(0.0)
    assert total_tts == pytest.approx(0.0)
    assert refund == pytest.approx((service_id, -10, -expected_cost))
    assert daily_refund == pytest.approx((0, 0, 0))


@pytest.mark.asyncio
async def test_tts_charge_never_makes_balance_negative(billing_db):
    assert await common.cost_tts(1, 101, provider="elevenlabs") is False

    with sqlite3.connect(billing_db) as conn:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id=1"
        ).fetchone()[0]
        usage_count = conn.execute("SELECT COUNT(*) FROM SERVICE_USAGE").fetchone()[0]

    assert balance == pytest.approx(1.0)
    assert usage_count == 0


@pytest.mark.asyncio
async def test_cost_initialize_keeps_both_provider_prices(monkeypatch):
    async def load_costs():
        return {
            "TTS_COST_PER_CHARACTER_ELEVENLABS": 0.03,
            "TTS_SERVICE_ID_ELEVENLABS": 31,
            "TTS_COST_PER_CHARACTER_OPENAI": 0.004,
            "TTS_SERVICE_ID_OPENAI": 42,
        }

    monkeypatch.setattr(common, "load_service_costs", load_costs)
    monkeypatch.setattr(common, "tts_engine", "elevenlabs")

    await common.Cost.initialize()

    assert common.Cost.get_tts_service("elevenlabs") == (0.03, 31)
    assert common.Cost.get_tts_service("openai") == (0.004, 42)
    assert common.Cost.get_tts_service() == (0.03, 31)


@pytest.mark.asyncio
async def test_cost_initialize_keeps_all_stt_provider_prices(monkeypatch):
    async def load_costs():
        return {
            "STT_COST_PER_MINUTE_ELEVENLABS": 0.007,
            "STT_SERVICE_ID_ELEVENLABS": 51,
            "STT_COST_PER_MINUTE_DEEPGRAM": 0.006,
            "STT_SERVICE_ID_DEEPGRAM": 52,
        }

    monkeypatch.setattr(common, "load_service_costs", load_costs)
    monkeypatch.setattr(common, "stt_engine", "deepgram")

    await common.Cost.initialize()

    assert common.Cost.get_stt_service("elevenlabs") == (0.007, 51)
    assert common.Cost.get_stt_service("deepgram") == (0.006, 52)
    assert common.Cost.get_stt_service() == (0.006, 52)


def test_explicit_stt_provider_never_falls_back_to_global_engine(monkeypatch):
    monkeypatch.setattr(common, "stt_engine", "deepgram")
    monkeypatch.setattr(
        common.Cost,
        "STT_PROVIDER_SERVICES",
        {
            "deepgram": {"cost_per_minute": 0.006, "service_id": 52},
        },
    )

    assert common.Cost.get_stt_service() == (0.006, 52)
    with pytest.raises(ValueError, match="elevenlabs"):
        common.Cost.get_stt_service("elevenlabs")


@pytest.mark.asyncio
async def test_stt_initialize_preserves_selected_legacy_generic_service(monkeypatch):
    async def load_costs():
        return {
            "STT_COST_PER_MINUTE": 0.008,
            "STT_SERVICE_ID": 61,
        }

    monkeypatch.setattr(common, "load_service_costs", load_costs)
    monkeypatch.setattr(common, "stt_engine", "deepgram")

    await common.Cost.initialize()

    assert common.Cost.get_stt_service("deepgram") == (0.008, 61)
    assert common.Cost.get_stt_service("elevenlabs") == (0.005, None)
    assert common.Cost.STT_COST_PER_MINUTE == pytest.approx(0.008)
    assert common.Cost.STT_SERVICE_ID == 61
