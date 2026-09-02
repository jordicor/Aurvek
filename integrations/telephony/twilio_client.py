"""Minimal asynchronous Twilio Programmable Voice client.

Creating a call is deliberately a single-attempt operation.  A timeout,
transport failure, 5xx response, or malformed success can mean Twilio accepted
the call even though Aurvek did not receive a usable response.  Those cases are
returned as ``dispatch_unknown`` and must be reconciled from callbacks; this
client never retries them.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


DEFAULT_STATUS_CALLBACK_EVENTS = (
    "initiated",
    "ringing",
    "answered",
    "completed",
)
_ALLOWED_STATUS_CALLBACK_EVENTS = frozenset(DEFAULT_STATUS_CALLBACK_EVENTS)
_CALL_SID_PATTERN = re.compile(r"CA[0-9a-fA-F]{32}")
_RECORDING_SID_PATTERN = re.compile(r"RE[0-9a-fA-F]{32}")
_NUMBER_SID_PATTERN = re.compile(r"PN[0-9a-fA-F]{32}")
_APPLICATION_SID_PATTERN = re.compile(r"AP[0-9a-fA-F]{32}")
_TRUNK_SID_PATTERN = re.compile(r"TK[0-9a-fA-F]{32}")
_E164_PATTERN = re.compile(r"\+[1-9][0-9]{7,14}")
MAX_NUMBER_INVENTORY_PAGES = 20
MAX_NUMBER_INVENTORY_ITEMS = 5_000
MAX_NUMBER_INVENTORY_PAGE_BYTES = 2_000_000
MAX_NUMBER_INVENTORY_TOTAL_BYTES = 8_000_000
NUMBER_INVENTORY_DEADLINE_SECONDS = 30.0
MAX_NUMBER_MUTATION_RESPONSE_BYTES = 1_000_000


class CallDispatchOutcome(StrEnum):
    ACCEPTED = "accepted"
    DISPATCH_UNKNOWN = "dispatch_unknown"


class DispatchUnknownReason(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class CreateCallRequest:
    from_e164: str
    to_e164: str
    twiml_url: str
    status_callback_url: str
    status_callback_events: tuple[str, ...] = DEFAULT_STATUS_CALLBACK_EVENTS
    amd_enabled: bool = False
    amd_status_callback_url: str | None = None

    def __post_init__(self) -> None:
        if not self.from_e164 or not self.to_e164:
            raise ValueError("from_e164 and to_e164 are required")
        _require_https_url(self.twiml_url, field_name="twiml_url")
        _require_https_url(
            self.status_callback_url,
            field_name="status_callback_url",
        )
        if not self.status_callback_events:
            raise ValueError("at least one status callback event is required")
        if len(set(self.status_callback_events)) != len(self.status_callback_events):
            raise ValueError("status callback events cannot be repeated")
        unknown = set(self.status_callback_events) - _ALLOWED_STATUS_CALLBACK_EVENTS
        if unknown:
            raise ValueError(f"unsupported status callback events: {sorted(unknown)}")
        if not isinstance(self.amd_enabled, bool):
            raise ValueError("amd_enabled must be a boolean")
        if self.amd_enabled:
            if not self.amd_status_callback_url:
                raise ValueError("amd_status_callback_url is required when AMD is enabled")
            _require_https_url(
                self.amd_status_callback_url,
                field_name="amd_status_callback_url",
            )
        elif self.amd_status_callback_url is not None:
            raise ValueError("amd_status_callback_url requires AMD to be enabled")


@dataclass(frozen=True, slots=True)
class CallDispatchResult:
    outcome: CallDispatchOutcome
    call_sid: str | None = None
    provider_status: str | None = None
    unknown_reason: DispatchUnknownReason | None = None
    http_status_code: int | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is CallDispatchOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class IncomingPhoneNumber:
    """Sanitized Voice-relevant fields from one Twilio number."""

    sid: str
    e164: str
    friendly_name: str | None
    iso_country: str | None
    region: str | None
    capabilities: dict[str, bool]
    voice_url: str | None
    voice_method: str | None
    status_callback_url: str | None = None
    status_callback_method: str | None = None
    voice_application_sid: str | None = None
    trunk_sid: str | None = None


class TwilioVoiceAPIError(RuntimeError):
    """A definitive client-side rejection returned by Twilio."""

    def __init__(
        self,
        *,
        status_code: int,
        provider_code: int | None,
        provider_message: str,
    ) -> None:
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_message = provider_message
        code_label = provider_code if provider_code is not None else "unknown"
        super().__init__(
            f"Twilio Voice API rejected the request "
            f"(HTTP {status_code}, code {code_label})"
        )


def _require_https_url(value: str, *, field_name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")


class AsyncTwilioVoiceClient:
    """Create Twilio calls with exactly one HTTP POST per method invocation."""

    _CALLS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    _RECORDINGS_URL = (
        "https://api.twilio.com/2010-04-01/Accounts/{sid}/Recordings.json"
    )
    _NUMBERS_URL = (
        "https://api.twilio.com/2010-04-01/Accounts/{sid}/"
        "IncomingPhoneNumbers.json"
    )

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not account_sid or not auth_token:
            raise ValueError("account_sid and auth_token are required")
        self.account_sid = account_sid
        self._auth = (account_sid, auth_token)
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = httpx.AsyncHTTPTransport(retries=0)
            self._client = httpx.AsyncClient(
                auth=self._auth,
                timeout=httpx.Timeout(30.0, connect=10.0, write=10.0, pool=10.0),
                transport=transport,
                trust_env=False,
            )
        return self._client

    async def create_call_once(
        self,
        request: CreateCallRequest,
    ) -> CallDispatchResult:
        """Issue one Calls API POST and classify any ambiguous result."""

        endpoint = self._CALLS_URL.format(sid=self.account_sid)
        # httpx treats a raw list of tuples as a synchronous request stream in
        # some supported releases.  Dict values may still be lists, producing
        # the repeated StatusCallbackEvent fields Twilio expects while keeping
        # the request fully asynchronous.
        form_data: dict[str, str | list[str]] = {
            "From": request.from_e164,
            "To": request.to_e164,
            "Url": request.twiml_url,
            "Method": "POST",
            "StatusCallback": request.status_callback_url,
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": list(request.status_callback_events),
        }
        if request.amd_enabled:
            form_data.update(
                {
                    "MachineDetection": "Enable",
                    "AsyncAmd": "true",
                    "AsyncAmdStatusCallback": str(request.amd_status_callback_url),
                    "AsyncAmdStatusCallbackMethod": "POST",
                }
            )

        try:
            response = await self._get_client().post(
                endpoint,
                data=form_data,
                auth=self._auth,
            )
        except httpx.TimeoutException:
            return CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.TIMEOUT,
            )
        except httpx.RequestError:
            return CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.TRANSPORT_ERROR,
            )

        if response.status_code >= 500:
            return CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.SERVER_ERROR,
                http_status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise _api_error_from_response(response)

        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("sid"), str):
            return CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.INVALID_RESPONSE,
                http_status_code=response.status_code,
            )

        call_sid = payload["sid"]
        if _CALL_SID_PATTERN.fullmatch(call_sid) is None:
            return CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.INVALID_RESPONSE,
                http_status_code=response.status_code,
            )
        provider_status = payload.get("status")
        return CallDispatchResult(
            outcome=CallDispatchOutcome.ACCEPTED,
            call_sid=call_sid,
            provider_status=(
                provider_status if isinstance(provider_status, str) else None
            ),
            http_status_code=response.status_code,
        )

    async def end_call_once(self, call_sid: str) -> bool:
        """Request a terminal call state once, without transport retries.

        A 404/410 is idempotent success: the provider no longer has a mutable
        call.  Ambiguous network/5xx outcomes are surfaced to the caller and
        never retried here.
        """

        if _CALL_SID_PATTERN.fullmatch(str(call_sid or "")) is None:
            raise ValueError("call_sid is invalid")
        endpoint = self._CALLS_URL.format(sid=self.account_sid).replace(
            "Calls.json", f"Calls/{call_sid}.json"
        )
        try:
            response = await self._get_client().post(
                endpoint,
                data={"Status": "completed"},
                auth=self._auth,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise RuntimeError("Twilio call termination outcome is unknown") from exc
        if response.status_code in {404, 410}:
            return False
        if response.status_code >= 500:
            raise RuntimeError("Twilio call termination outcome is unknown")
        if response.status_code >= 400:
            raise _api_error_from_response(response)
        if not 200 <= response.status_code < 300:
            raise RuntimeError("Twilio call termination outcome is unknown")
        return True

    async def delete_call_record_once(self, call_sid: str) -> bool:
        """Delete one terminal Twilio Call resource without implicit retries.

        A missing resource is verified idempotent success.  Transport failures
        and 5xx responses remain ambiguous and must be retained durably by the
        caller rather than retried blindly inside this client.
        """

        if _CALL_SID_PATTERN.fullmatch(str(call_sid or "")) is None:
            raise ValueError("call_sid is invalid")
        endpoint = self._CALLS_URL.format(sid=self.account_sid).replace(
            "Calls.json", f"Calls/{call_sid}.json"
        )
        return await self._delete_resource_once(
            endpoint,
            unavailable_message="Twilio call deletion outcome is unknown",
        )

    async def delete_recording_once(self, recording_sid: str) -> bool:
        """Delete one Twilio Recording and require a definitive HTTP outcome."""

        if _RECORDING_SID_PATTERN.fullmatch(str(recording_sid or "")) is None:
            raise ValueError("recording_sid is invalid")
        endpoint = self._RECORDINGS_URL.format(sid=self.account_sid).replace(
            "Recordings.json", f"Recordings/{recording_sid}.json"
        )
        return await self._delete_resource_once(
            endpoint,
            unavailable_message="Twilio recording deletion outcome is unknown",
        )

    async def _delete_resource_once(
        self,
        endpoint: str,
        *,
        unavailable_message: str,
    ) -> bool:
        try:
            response = await self._get_client().delete(endpoint, auth=self._auth)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise RuntimeError(unavailable_message) from exc
        if response.status_code in {404, 410}:
            return False
        if response.status_code >= 500:
            raise RuntimeError(unavailable_message)
        if response.status_code >= 400:
            raise _api_error_from_response(response)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(unavailable_message)
        return True

    async def list_incoming_phone_numbers(self) -> tuple[IncomingPhoneNumber, ...]:
        """Return the complete owned-number inventory without exposing credentials."""

        try:
            async with asyncio.timeout(NUMBER_INVENTORY_DEADLINE_SECONDS):
                return await self._list_incoming_phone_numbers_bounded()
        except TimeoutError as exc:
            raise RuntimeError("Twilio number inventory exceeded its deadline") from exc

    async def _list_incoming_phone_numbers_bounded(
        self,
    ) -> tuple[IncomingPhoneNumber, ...]:
        """Fetch inventory within strict page, item, and body budgets."""

        base_url = self._NUMBERS_URL.format(sid=self.account_sid)
        next_url: str | None = f"{base_url}?PageSize=1000"
        numbers: list[IncomingPhoneNumber] = []
        seen_pages: set[str] = set()
        total_bytes = 0
        while next_url is not None:
            if len(seen_pages) >= MAX_NUMBER_INVENTORY_PAGES:
                raise RuntimeError("Twilio number inventory exceeded its page limit")
            _require_twilio_account_url(
                next_url,
                account_sid=self.account_sid,
                resource="IncomingPhoneNumbers",
            )
            if next_url in seen_pages:
                raise RuntimeError("Twilio number inventory pagination looped")
            seen_pages.add(next_url)
            client = self._get_client()
            request = client.build_request("GET", next_url)
            try:
                response = await client.send(
                    request,
                    auth=self._auth,
                    stream=True,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise RuntimeError("Twilio number inventory is unavailable") from exc
            try:
                body = await _read_limited_response(
                    response,
                    maximum_bytes=min(
                        MAX_NUMBER_INVENTORY_PAGE_BYTES,
                        MAX_NUMBER_INVENTORY_TOTAL_BYTES - total_bytes,
                    ),
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise RuntimeError("Twilio number inventory is unavailable") from exc
            finally:
                await response.aclose()
            body_size = len(body)
            total_bytes += body_size
            if response.status_code >= 400:
                raise _api_error_from_response(
                    httpx.Response(
                        response.status_code,
                        content=body,
                        request=request,
                    )
                )
            try:
                payload = json.loads(body)
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("Twilio number inventory response is invalid") from exc
            if not isinstance(payload, dict) or not isinstance(
                payload.get("incoming_phone_numbers"), list
            ):
                raise RuntimeError("Twilio number inventory response is invalid")
            raw_numbers = payload["incoming_phone_numbers"]
            if len(numbers) + len(raw_numbers) > MAX_NUMBER_INVENTORY_ITEMS:
                raise RuntimeError("Twilio number inventory exceeded its item limit")
            numbers.extend(_incoming_number_from_payload(item) for item in raw_numbers)
            raw_next = payload.get("next_page_uri")
            if raw_next in {None, ""}:
                next_url = None
            elif isinstance(raw_next, str):
                next_url = urljoin("https://api.twilio.com", raw_next)
            else:
                raise RuntimeError("Twilio number inventory pagination is invalid")
        return tuple(numbers)

    async def update_incoming_number_voice(
        self,
        number_sid: str,
        *,
        voice_url: str,
        voice_method: str = "POST",
        status_callback_url: str,
        status_callback_method: str = "POST",
    ) -> IncomingPhoneNumber:
        """Configure only Programmable Voice fields for one owned number."""

        normalized_sid = str(number_sid or "").strip()
        if _NUMBER_SID_PATTERN.fullmatch(normalized_sid) is None:
            raise ValueError("number_sid is invalid")
        _require_https_url(voice_url, field_name="voice_url")
        _require_https_url(
            status_callback_url,
            field_name="status_callback_url",
        )
        method = str(voice_method or "").strip().upper()
        callback_method = str(status_callback_method or "").strip().upper()
        if method not in {"GET", "POST"}:
            raise ValueError("voice_method must be GET or POST")
        if callback_method not in {"GET", "POST"}:
            raise ValueError("status_callback_method must be GET or POST")
        endpoint = self._NUMBERS_URL.format(sid=self.account_sid).replace(
            "IncomingPhoneNumbers.json",
            f"IncomingPhoneNumbers/{normalized_sid}.json",
        )
        try:
            response = await self._get_client().post(
                endpoint,
                data={
                    "VoiceUrl": voice_url,
                    "VoiceMethod": method,
                    "StatusCallback": status_callback_url,
                    "StatusCallbackMethod": callback_method,
                    # Empty SID values select direct VoiceUrl routing.  Both
                    # alternatives are cleared atomically; no Messaging field
                    # is included in this narrowly scoped mutation.
                    "VoiceApplicationSid": "",
                    "TrunkSid": "",
                },
                auth=self._auth,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise RuntimeError("Twilio number Voice configuration is unavailable") from exc
        if response.status_code >= 400:
            raise _api_error_from_response(response)
        if len(response.content) > MAX_NUMBER_MUTATION_RESPONSE_BYTES:
            raise RuntimeError("Twilio number response is too large")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Twilio number response is invalid") from exc
        return _incoming_number_from_payload(
            payload,
            require_explicit_routing_fields=True,
        )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


async def _read_limited_response(
    response: httpx.Response,
    *,
    maximum_bytes: int,
) -> bytes:
    if maximum_bytes <= 0:
        raise RuntimeError("Twilio number inventory response is too large")
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise RuntimeError(
                "Twilio number inventory response is invalid"
            ) from exc
        if declared_length < 0:
            raise RuntimeError("Twilio number inventory response is invalid")
        if declared_length > maximum_bytes:
            raise RuntimeError("Twilio number inventory response is too large")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > maximum_bytes:
            raise RuntimeError("Twilio number inventory response is too large")
        body.extend(chunk)
    return bytes(body)


def _api_error_from_response(response: httpx.Response) -> TwilioVoiceAPIError:
    payload: dict[str, Any] = {}
    try:
        decoded = response.json()
        if isinstance(decoded, dict):
            payload = decoded
    except (ValueError, TypeError):
        pass

    raw_code = payload.get("code")
    try:
        provider_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        provider_code = None
    raw_message = payload.get("message")
    provider_message = (
        raw_message if isinstance(raw_message, str) else "Request rejected by Twilio"
    )
    return TwilioVoiceAPIError(
        status_code=response.status_code,
        provider_code=provider_code,
        provider_message=provider_message,
    )


def _incoming_number_from_payload(
    value: Any,
    *,
    require_explicit_routing_fields: bool = False,
) -> IncomingPhoneNumber:
    if not isinstance(value, dict):
        raise RuntimeError("Twilio number inventory item is invalid")
    if require_explicit_routing_fields and (
        "voice_application_sid" not in value or "trunk_sid" not in value
    ):
        raise RuntimeError(
            "Twilio number mutation did not confirm alternate Voice routing"
        )
    sid = str(value.get("sid") or "").strip()
    e164 = str(value.get("phone_number") or "").strip()
    if _NUMBER_SID_PATTERN.fullmatch(sid) is None or _E164_PATTERN.fullmatch(e164) is None:
        raise RuntimeError("Twilio number inventory item has an invalid identity")
    capabilities_value = value.get("capabilities")
    if capabilities_value is None:
        capabilities_value = {}
    if not isinstance(capabilities_value, dict):
        raise RuntimeError("Twilio number capabilities are invalid")
    capabilities = {
        str(key): bool(item)
        for key, item in capabilities_value.items()
        if isinstance(key, str) and isinstance(item, bool)
    }
    voice_method_value = value.get("voice_method")
    voice_method = (
        str(voice_method_value).strip().upper()
        if voice_method_value is not None
        else None
    )
    if voice_method not in {None, "", "GET", "POST"}:
        raise RuntimeError("Twilio number Voice method is invalid")
    voice_url_value = value.get("voice_url")
    voice_url = str(voice_url_value).strip() if voice_url_value else None
    if voice_url:
        _require_provider_url(voice_url, field_name="voice_url")
    status_callback_value = value.get("status_callback")
    status_callback_url = (
        str(status_callback_value).strip() if status_callback_value else None
    )
    if status_callback_url:
        _require_provider_url(
            status_callback_url,
            field_name="status_callback_url",
        )
    status_callback_method_value = value.get("status_callback_method")
    status_callback_method = (
        str(status_callback_method_value).strip().upper()
        if status_callback_method_value is not None
        else None
    )
    if status_callback_method not in {None, "", "GET", "POST"}:
        raise RuntimeError("Twilio number Status callback method is invalid")

    def optional_routing_sid(key: str, pattern: re.Pattern[str]) -> str | None:
        raw = value.get(key)
        if raw is None or raw == "":
            return None
        sid_value = str(raw).strip()
        if pattern.fullmatch(sid_value) is None:
            raise RuntimeError(f"Twilio number {key} is invalid")
        return sid_value

    voice_application_sid = optional_routing_sid(
        "voice_application_sid", _APPLICATION_SID_PATTERN
    )
    trunk_sid = optional_routing_sid("trunk_sid", _TRUNK_SID_PATTERN)

    def optional_text(key: str, maximum: int) -> str | None:
        raw = value.get(key)
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if len(text) > maximum or any(ord(character) < 32 for character in text):
            raise RuntimeError(f"Twilio number {key} is invalid")
        return text

    country = optional_text("iso_country", 2)
    if country is not None:
        country = country.upper()
        if len(country) != 2 or not country.isalpha():
            raise RuntimeError("Twilio number country is invalid")
    return IncomingPhoneNumber(
        sid=sid,
        e164=e164,
        friendly_name=optional_text("friendly_name", 200),
        iso_country=country,
        region=optional_text("region", 100),
        capabilities=capabilities,
        voice_url=voice_url,
        voice_method=voice_method or None,
        status_callback_url=status_callback_url,
        status_callback_method=status_callback_method or None,
        voice_application_sid=voice_application_sid,
        trunk_sid=trunk_sid,
    )


def _require_twilio_account_url(
    value: str,
    *,
    account_sid: str,
    resource: str,
) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Twilio pagination URL is invalid") from exc
    expected_prefix = f"/2010-04-01/Accounts/{account_sid}/{resource}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.twilio.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != f"{expected_prefix}.json"
        or parsed.fragment
    ):
        raise RuntimeError("Twilio pagination URL is invalid")


def _require_provider_url(value: str, *, field_name: str) -> None:
    """Validate a provider-reported URL without requiring it to be usable.

    Existing Twilio inventory may contain an old HTTP Voice URL.  Admin must be
    able to display that drift and repair it; only new Aurvek callback writes
    are required to use HTTPS.
    """

    if len(value) > 2_048 or any(ord(character) < 32 for character in value):
        raise RuntimeError(f"Twilio number {field_name} is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError(f"Twilio number {field_name} is invalid")


__all__ = [
    "AsyncTwilioVoiceClient",
    "CallDispatchOutcome",
    "CallDispatchResult",
    "CreateCallRequest",
    "DispatchUnknownReason",
    "IncomingPhoneNumber",
    "TwilioVoiceAPIError",
]
