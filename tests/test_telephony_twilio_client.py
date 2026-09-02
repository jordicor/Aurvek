from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

import integrations.telephony.twilio_client as twilio_client_module
from integrations.telephony.twilio_client import (
    AsyncTwilioVoiceClient,
    CallDispatchOutcome,
    CreateCallRequest,
    DispatchUnknownReason,
    TwilioVoiceAPIError,
)


VALID_CALL_SID = "CA0123456789abcdefABCDEF0123456789"
VALID_NUMBER_SID = "PN0123456789abcdefABCDEF0123456789"


def make_request(**overrides) -> CreateCallRequest:
    values = {
        "from_e164": "+13055550100",
        "to_e164": "+34600111222",
        "twiml_url": "https://aurvek.example/webhooks/twilio/voice/twiml/token",
        "status_callback_url": (
            "https://aurvek.example/webhooks/twilio/voice/status"
        ),
    }
    values.update(overrides)
    return CreateCallRequest(**values)


@pytest.mark.asyncio
async def test_create_call_posts_once_with_all_status_callbacks() -> None:
    requests: list[httpx.Request] = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"sid": VALID_CALL_SID, "status": "queued"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient(
        "AC123",
        "auth-token",
        client=http_client,
    )
    try:
        result = await voice_client.create_call_once(make_request())
    finally:
        await http_client.aclose()

    assert result.outcome is CallDispatchOutcome.ACCEPTED
    assert result.accepted is True
    assert result.call_sid == VALID_CALL_SID
    assert result.provider_status == "queued"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url == (
        "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json"
    )
    form = parse_qs(requests[0].content.decode("ascii"))
    assert form == {
        "From": ["+13055550100"],
        "To": ["+34600111222"],
        "Url": ["https://aurvek.example/webhooks/twilio/voice/twiml/token"],
        "Method": ["POST"],
        "StatusCallback": [
            "https://aurvek.example/webhooks/twilio/voice/status"
        ],
        "StatusCallbackMethod": ["POST"],
        "StatusCallbackEvent": [
            "initiated",
            "ringing",
            "answered",
            "completed",
        ],
    }


@pytest.mark.asyncio
async def test_amd_is_explicit_and_uses_async_callback() -> None:
    requests = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"sid": VALID_CALL_SID}, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "auth-token", client=http_client)
    try:
        await voice_client.create_call_once(
            make_request(
                amd_enabled=True,
                amd_status_callback_url=(
                    "https://aurvek.example/webhooks/twilio/voice/amd/token"
                ),
            )
        )
    finally:
        await http_client.aclose()

    form = parse_qs(requests[0].content.decode("ascii"))
    assert form["MachineDetection"] == ["Enable"]
    assert form["AsyncAmd"] == ["true"]
    assert form["AsyncAmdStatusCallback"] == [
        "https://aurvek.example/webhooks/twilio/voice/amd/token"
    ]
    assert form["AsyncAmdStatusCallbackMethod"] == ["POST"]


@pytest.mark.asyncio
async def test_end_call_is_one_idempotent_provider_update() -> None:
    requests = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sid": VALID_CALL_SID}, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "auth-token", client=http_client)
    try:
        changed = await voice_client.end_call_once(VALID_CALL_SID)
    finally:
        await http_client.aclose()

    assert changed is True
    assert len(requests) == 1
    assert requests[0].url.path.endswith(f"/Calls/{VALID_CALL_SID}.json")
    assert parse_qs(requests[0].content.decode("ascii")) == {"Status": ["completed"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 410])
async def test_end_call_provider_absence_is_definitive(status_code) -> None:
    attempts = 0

    def provider_response(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "auth-token", client=http_client)
    try:
        changed = await voice_client.end_call_once(VALID_CALL_SID)
    finally:
        await http_client.aclose()

    assert changed is False
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [302, 307])
async def test_end_call_redirect_is_never_treated_as_accepted(status_code) -> None:
    attempts = 0

    def provider_response(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            status_code,
            headers={"Location": "https://example.invalid/redirect"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "auth-token", client=http_client)
    try:
        with pytest.raises(RuntimeError, match="outcome is unknown"):
            await voice_client.end_call_once(VALID_CALL_SID)
    finally:
        await http_client.aclose()

    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (httpx.ReadTimeout("no response"), DispatchUnknownReason.TIMEOUT),
        (
            httpx.ConnectError("connection failed"),
            DispatchUnknownReason.TRANSPORT_ERROR,
        ),
    ],
)
async def test_timeout_or_transport_error_is_unknown_and_never_retried(
    exception,
    reason,
) -> None:
    attempts = 0

    def provider_response(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        exception.request = request
        raise exception

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient(
        "AC123",
        "auth-token",
        client=http_client,
    )
    try:
        result = await voice_client.create_call_once(make_request())
    finally:
        await http_client.aclose()

    assert attempts == 1
    assert result.outcome is CallDispatchOutcome.DISPATCH_UNKNOWN
    assert result.unknown_reason is reason
    assert result.call_sid is None


@pytest.mark.asyncio
async def test_server_error_is_unknown_and_never_retried() -> None:
    attempts = 0

    def provider_response(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            500,
            json={"code": 20500, "message": "Internal Server Error"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient(
        "AC123",
        "auth-token",
        client=http_client,
    )
    try:
        result = await voice_client.create_call_once(make_request())
    finally:
        await http_client.aclose()

    assert attempts == 1
    assert result.outcome is CallDispatchOutcome.DISPATCH_UNKNOWN
    assert result.unknown_reason is DispatchUnknownReason.SERVER_ERROR
    assert result.http_status_code == 500


@pytest.mark.asyncio
async def test_client_error_is_a_definitive_rejection() -> None:
    def provider_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": 21211, "message": "Invalid destination"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient(
        "AC123",
        "auth-token",
        client=http_client,
    )
    try:
        with pytest.raises(TwilioVoiceAPIError) as captured:
            await voice_client.create_call_once(make_request())
    finally:
        await http_client.aclose()

    assert captured.value.status_code == 400
    assert captured.value.provider_code == 21211
    assert captured.value.provider_message == "Invalid destination"
    assert "Invalid destination" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(200, text="not-json", request=request),
        lambda request: httpx.Response(202, json={"status": "queued"}, request=request),
        lambda request: httpx.Response(299, json={"sid": ""}, request=request),
        lambda request: httpx.Response(201, json={"sid": 123}, request=request),
        lambda request: httpx.Response(201, json={"sid": "CA123"}, request=request),
        lambda request: httpx.Response(
            201,
            json={"sid": "AC0123456789abcdefABCDEF0123456789"},
            request=request,
        ),
        lambda request: httpx.Response(
            201,
            json={"sid": "ca0123456789abcdefABCDEF0123456789"},
            request=request,
        ),
        lambda request: httpx.Response(
            201,
            json={"sid": "CA0123456789abcdefABCDEF012345678"},
            request=request,
        ),
        lambda request: httpx.Response(
            201,
            json={"sid": "CA0123456789abcdefABCDEF01234567890"},
            request=request,
        ),
        lambda request: httpx.Response(
            201,
            json={"sid": "CA0123456789abcdefABCDEF012345678g"},
            request=request,
        ),
        lambda request: httpx.Response(
            201,
            json={"sid": f" {VALID_CALL_SID} "},
            request=request,
        ),
    ],
)
async def test_unusable_success_response_is_dispatch_unknown(response) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    voice_client = AsyncTwilioVoiceClient(
        "AC123",
        "auth-token",
        client=http_client,
    )
    try:
        result = await voice_client.create_call_once(make_request())
    finally:
        await http_client.aclose()

    assert result.outcome is CallDispatchOutcome.DISPATCH_UNKNOWN
    assert result.unknown_reason is DispatchUnknownReason.INVALID_RESPONSE


@pytest.mark.parametrize(
    "overrides",
    [
        {"twiml_url": "http://aurvek.example/twiml"},
        {"status_callback_url": "https://user:secret@aurvek.example/status"},
        {"status_callback_events": ()},
        {"status_callback_events": ("initiated", "initiated")},
        {"status_callback_events": ("queued",)},
    ],
)
def test_create_call_request_rejects_unsafe_or_unknown_options(overrides) -> None:
    with pytest.raises(ValueError):
        make_request(**overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routing_field", "routing_sid"),
    (
        ("voice_application_sid", "AP" + "4" * 32),
        ("trunk_sid", "TK" + "5" * 32),
    ),
)
async def test_number_inventory_paginates_and_voice_update_is_narrow(
    routing_field, routing_sid
) -> None:
    requests: list[httpx.Request] = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        number = {
            "sid": VALID_NUMBER_SID,
            "phone_number": "+13055550123",
            "friendly_name": "Miami line",
            "iso_country": "US",
            "region": "FL",
            "capabilities": {"voice": True, "sms": True},
            "voice_url": "https://old.example/voice",
            "voice_method": "POST",
            "status_callback": "https://old.example/status",
            "status_callback_method": "POST",
            routing_field: routing_sid,
        }
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"incoming_phone_numbers": [number], "next_page_uri": None},
                request=request,
            )
        updated = dict(number)
        updated["voice_url"] = "https://aurvek.example/webhooks/twilio/voice/inbound"
        updated["status_callback"] = (
            "https://aurvek.example/webhooks/twilio/voice/inbound-status"
        )
        updated["voice_application_sid"] = None
        updated["trunk_sid"] = None
        return httpx.Response(200, json=updated, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "credential", client=http_client)
    try:
        inventory = await voice_client.list_incoming_phone_numbers()
        updated = await voice_client.update_incoming_number_voice(
            VALID_NUMBER_SID,
            voice_url="https://aurvek.example/webhooks/twilio/voice/inbound",
            status_callback_url=(
                "https://aurvek.example/webhooks/twilio/voice/inbound-status"
            ),
        )
    finally:
        await http_client.aclose()

    assert len(inventory) == 1
    assert inventory[0].e164 == "+13055550123"
    assert inventory[0].capabilities == {"voice": True, "sms": True}
    assert getattr(inventory[0], routing_field) == routing_sid
    assert updated.voice_url.endswith("/webhooks/twilio/voice/inbound")
    assert updated.status_callback_url.endswith(
        "/webhooks/twilio/voice/inbound-status"
    )
    assert len(requests) == 2
    assert requests[0].method == "GET"
    assert requests[0].url.params["PageSize"] == "1000"
    assert requests[1].method == "POST"
    assert parse_qs(
        requests[1].content.decode("ascii"), keep_blank_values=True
    ) == {
        "VoiceUrl": ["https://aurvek.example/webhooks/twilio/voice/inbound"],
        "VoiceMethod": ["POST"],
        "StatusCallback": [
            "https://aurvek.example/webhooks/twilio/voice/inbound-status"
        ],
        "StatusCallbackMethod": ["POST"],
        "VoiceApplicationSid": [""],
        "TrunkSid": [""],
    }
    assert "Messaging" not in requests[1].content.decode("ascii")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "omitted_routing_fields",
    (
        ("voice_application_sid", "trunk_sid"),
        ("voice_application_sid",),
        ("trunk_sid",),
    ),
)
async def test_number_voice_update_requires_explicit_routing_confirmation(
    omitted_routing_fields,
) -> None:
    def provider_response(request: httpx.Request) -> httpx.Response:
        payload = {
            "sid": VALID_NUMBER_SID,
            "phone_number": "+13055550123",
            "voice_url": "https://aurvek.example/webhooks/twilio/voice/inbound",
            "voice_method": "POST",
            "status_callback": (
                "https://aurvek.example/webhooks/twilio/voice/inbound-status"
            ),
            "status_callback_method": "POST",
            "voice_application_sid": None,
            "trunk_sid": None,
        }
        for field in omitted_routing_fields:
            payload.pop(field)
        return httpx.Response(200, json=payload, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "credential", client=http_client)
    try:
        with pytest.raises(RuntimeError, match="did not confirm"):
            await voice_client.update_incoming_number_voice(
                VALID_NUMBER_SID,
                voice_url="https://aurvek.example/webhooks/twilio/voice/inbound",
                status_callback_url=(
                    "https://aurvek.example/webhooks/twilio/voice/inbound-status"
                ),
            )
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_number_inventory_safely_tolerates_omitted_routing_fields() -> None:
    def provider_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "incoming_phone_numbers": [
                    {
                        "sid": VALID_NUMBER_SID,
                        "phone_number": "+13055550123",
                        "capabilities": {"voice": True},
                    }
                ],
                "next_page_uri": None,
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "credential", client=http_client)
    try:
        inventory = await voice_client.list_incoming_phone_numbers()
    finally:
        await http_client.aclose()

    assert inventory[0].voice_application_sid is None
    assert inventory[0].trunk_sid is None


@pytest.mark.asyncio
async def test_number_inventory_rejects_untrusted_next_page() -> None:
    def provider_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "incoming_phone_numbers": [],
                "next_page_uri": "https://evil.example/steal",
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "credential", client=http_client)
    try:
        with pytest.raises(RuntimeError, match="pagination URL"):
            await voice_client.list_incoming_phone_numbers()
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["pages", "items", "bytes"])
async def test_number_inventory_enforces_resource_budgets(
    monkeypatch,
    budget,
) -> None:
    number = {
        "sid": VALID_NUMBER_SID,
        "phone_number": "+13055550123",
        "capabilities": {"voice": True},
    }

    def provider_response(request: httpx.Request) -> httpx.Response:
        next_uri = (
            "/2010-04-01/Accounts/AC123/IncomingPhoneNumbers.json?Page=2"
            if budget == "pages"
            else None
        )
        return httpx.Response(
            200,
            json={
                "incoming_phone_numbers": [number],
                "next_page_uri": next_uri,
            },
            request=request,
        )

    if budget == "pages":
        monkeypatch.setattr(twilio_client_module, "MAX_NUMBER_INVENTORY_PAGES", 1)
    elif budget == "items":
        monkeypatch.setattr(twilio_client_module, "MAX_NUMBER_INVENTORY_ITEMS", 0)
    else:
        monkeypatch.setattr(
            twilio_client_module,
            "MAX_NUMBER_INVENTORY_PAGE_BYTES",
            1,
        )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_response))
    voice_client = AsyncTwilioVoiceClient("AC123", "credential", client=http_client)
    try:
        with pytest.raises(RuntimeError, match="exceeded|too large"):
            await voice_client.list_incoming_phone_numbers()
    finally:
        await http_client.aclose()
