from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading

import requests

from tools import tts_load_balancer as load_balancer


class VoiceResponse:
    def __init__(self, status_code: int, voice_id: str | None = None) -> None:
        self.status_code = status_code
        self._voice_id = voice_id

    def json(self):
        return {"voice_id": self._voice_id}


def _ready_manager(*keys: str) -> load_balancer.APIKeyManager:
    manager = load_balancer.APIKeyManager()
    now = datetime.now()
    for value in keys:
        manager.add_key(value)
    for api_key in manager.api_keys:
        api_key.available_chars = 10_000
        api_key.reset_date = now + timedelta(days=10)
    manager.last_update = now
    manager.last_attempt = now
    manager._ready = True
    return manager


def test_voice_routing_skips_partial_catalogs_and_caches_both_answers(
    monkeypatch,
):
    manager = _ready_manager("synthetic-key-a", "synthetic-key-b")
    requests_seen = []

    def get(url, *, headers, timeout):
        requests_seen.append((url, headers["xi-api-key"], timeout))
        if headers["xi-api-key"] == "synthetic-key-a":
            return VoiceResponse(404)
        return VoiceResponse(200, "voice-only-on-b")

    monkeypatch.setattr(load_balancer.requests, "get", get)
    monkeypatch.setattr(load_balancer.random, "uniform", lambda _low, _high: 0)

    first = manager.select_key(voice_id="voice-only-on-b")
    second = manager.select_key(voice_id="voice-only-on-b")

    assert first is manager.api_keys[1]
    assert second is manager.api_keys[1]
    assert requests_seen == [
        (
            "https://api.elevenlabs.io/v1/voices/voice-only-on-b",
            "synthetic-key-a",
            (5, 15),
        ),
        (
            "https://api.elevenlabs.io/v1/voices/voice-only-on-b",
            "synthetic-key-b",
            (5, 15),
        ),
    ]


def test_concurrent_cold_refresh_updates_each_key_once():
    manager = load_balancer.APIKeyManager()
    manager.add_key("synthetic-key")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def update_info():
        calls.append("update")
        entered.set()
        assert release.wait(timeout=2)

    manager.api_keys[0].update_info = update_info
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(manager.update_all_keys)
        assert entered.wait(timeout=2)
        second = executor.submit(manager.update_all_keys)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert calls == ["update"]


def test_prepare_refreshes_cold_pool_and_returns_only_readiness():
    manager = load_balancer.APIKeyManager()
    for index in range(4):
        manager.add_key(f"synthetic-key-{index}")
    calls = []

    for index, api_key in enumerate(manager.api_keys):
        def update_info(*, current=api_key, position=index):
            calls.append(position)
            current.available_chars = 10_000
            current.reset_date = datetime.now() + timedelta(days=10)

        api_key.update_info = update_info

    result = manager.prepare()

    assert result is True
    assert calls == [0, 1, 2, 3]
    assert manager.last_update is not None


def test_prepare_returns_false_when_no_key_metadata_is_usable():
    manager = load_balancer.APIKeyManager()
    manager.add_key("synthetic-key")
    manager.api_keys[0].update_info = lambda: None

    assert manager.prepare() is False


def test_failed_prepare_retries_after_short_backoff_then_caches_success():
    manager = load_balancer.APIKeyManager()
    manager.add_key("synthetic-key")
    calls = 0

    def update_info():
        nonlocal calls
        calls += 1
        if calls == 1:
            manager.api_keys[0].reset_date = None
            return
        manager.api_keys[0].available_chars = 10_000
        manager.api_keys[0].reset_date = datetime.now() + timedelta(days=10)

    manager.api_keys[0].update_info = update_info

    assert manager.prepare() is False
    assert manager.is_ready() is False
    assert manager.prepare() is False
    assert calls == 1

    manager.last_attempt -= timedelta(seconds=31)
    assert manager.prepare() is True
    assert manager.is_ready() is True
    assert calls == 2

    manager.last_attempt -= timedelta(minutes=29)
    assert manager.prepare() is True
    assert calls == 2


def test_cached_readiness_never_waits_for_an_in_flight_provider_refresh():
    manager = _ready_manager("synthetic-key")
    manager.last_attempt -= timedelta(minutes=31)
    entered = threading.Event()
    release = threading.Event()

    def update_info():
        entered.set()
        assert release.wait(timeout=2)
        manager.api_keys[0].reset_date = None

    manager.api_keys[0].update_info = update_info
    with ThreadPoolExecutor(max_workers=1) as executor:
        refresh = executor.submit(manager.prepare)
        assert entered.wait(timeout=2)
        assert manager.is_ready() is True
        release.set()
        assert refresh.result(timeout=2) is False

    assert manager.is_ready() is False


def test_voice_routing_fails_closed_and_does_not_cache_network_or_server_errors(
    monkeypatch,
):
    manager = _ready_manager("synthetic-key-a", "synthetic-key-b")
    calls = []

    def get(_url, *, headers, timeout):
        calls.append((headers["xi-api-key"], timeout))
        if headers["xi-api-key"] == "synthetic-key-a":
            raise requests.ConnectionError("synthetic outage")
        return VoiceResponse(503)

    monkeypatch.setattr(load_balancer.requests, "get", get)
    monkeypatch.setattr(load_balancer.random, "uniform", lambda _low, _high: 0)

    assert manager.select_key(voice_id="voice-id") is None
    assert manager.select_key(voice_id="voice-id") is None
    assert calls == [
        ("synthetic-key-a", (5, 15)),
        ("synthetic-key-b", (5, 15)),
        ("synthetic-key-a", (5, 15)),
        ("synthetic-key-b", (5, 15)),
    ]


def test_network_failure_on_one_key_does_not_hide_a_verified_compatible_key(
    monkeypatch,
):
    manager = _ready_manager("synthetic-key-a", "synthetic-key-b")

    def get(_url, *, headers, timeout):
        assert timeout == (5, 15)
        if headers["xi-api-key"] == "synthetic-key-a":
            raise requests.ConnectionError("synthetic outage")
        return VoiceResponse(200, "voice-id")

    monkeypatch.setattr(load_balancer.requests, "get", get)
    monkeypatch.setattr(load_balancer.random, "uniform", lambda _low, _high: 0)

    assert manager.select_key(voice_id="voice-id") is manager.api_keys[1]


def test_voice_routing_accepts_documented_legacy_voice_resolution_and_caches_it(
    monkeypatch,
):
    manager = _ready_manager("synthetic-key")
    calls = []

    def get(*_args, **_kwargs):
        calls.append("probe")
        return VoiceResponse(200, "canonical-replacement")

    monkeypatch.setattr(
        load_balancer.requests,
        "get",
        get,
    )

    assert manager.select_key(voice_id="legacy-voice") is manager.api_keys[0]
    assert manager.select_key(voice_id="legacy-voice") is manager.api_keys[0]
    assert calls == ["probe"]


def test_voice_routing_rejects_success_without_a_valid_voice_identity(monkeypatch):
    manager = _ready_manager("synthetic-key")

    for invalid_voice_id in (None, "", " ", 123):
        monkeypatch.setattr(
            load_balancer.requests,
            "get",
            lambda *_args, _value=invalid_voice_id, **_kwargs: VoiceResponse(
                200, _value
            ),
        )

        assert manager.select_key(voice_id="requested-voice") is None


def test_legacy_selection_does_not_probe_voice_catalog(monkeypatch):
    manager = _ready_manager("legacy-key")

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("legacy selection must not probe a voice")

    monkeypatch.setattr(load_balancer.requests, "get", must_not_probe)
    monkeypatch.setattr(load_balancer, "api_key_manager", manager)

    assert manager.select_key() is manager.api_keys[0]
    assert load_balancer.get_elevenlabs_key() == "legacy-key"


def test_invalid_voice_id_never_falls_back_to_an_arbitrary_key(monkeypatch):
    manager = _ready_manager("legacy-key")
    monkeypatch.setattr(load_balancer, "api_key_manager", manager)

    assert load_balancer.get_elevenlabs_key(voice_id=" ") is None
    assert manager.api_keys[0].last_used is None
