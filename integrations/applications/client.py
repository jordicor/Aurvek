"""Small server client; this file can be copied without the Aurvek runtime.

Requires httpx. Keep the client secret and delegated credentials on your server.
Mutating calls are not retried automatically: retain their operation_id when
retrying a request whose outcome is uncertain.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

import httpx

CONTRACT = "aurvek_applications.v1"
INTERVIEW_CONTRACT = "aurvek_embed.v1"


def _bounded_string(value, name: str, minimum: int, maximum: int, pattern=None):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or (pattern is not None and not re.fullmatch(pattern, value))):
        raise ValueError(f"Invalid {name}")
    return value


def validate_interview_brief(brief: dict) -> dict:
    """Validate the embed InterviewBrief wire schema without importing Aurvek."""
    limits = {"preferred_name": 160, "scope_text": 4000, "purpose_text": 4000,
              "audience_text": 4000, "boundaries_text": 4000}
    if not isinstance(brief, dict) or set(brief) - ({"schema_version", "revision", "language"} | limits.keys()):
        raise ValueError("Invalid interview brief")
    schema_version = brief.get("schema_version", 1)
    revision = brief.get("revision")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("Invalid interview brief schema_version")
    if type(revision) is not int or revision < 1:
        raise ValueError("Invalid interview brief revision")
    language = _bounded_string(brief.get("language"), "interview brief language", 2, 64,
                               r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
    result = {"schema_version": schema_version, "revision": revision, "language": language}
    for name, maximum in limits.items():
        value = brief.get(name)
        result[name] = None if value is None else _bounded_string(value, f"interview brief {name}", 0, maximum)
    return result


class AurvekError(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(f"Aurvek {status_code}: {code}")
        self.code, self.status_code = code, status_code


class AurvekClient:
    def __init__(self, issuer: str, client_id: str, client_secret: str, *,
                 loopback_url: str | None = None, timeout: float = 30, transport=None):
        url = urlsplit(issuer)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("issuer must be an HTTPS origin")
        endpoint = issuer
        if loopback_url:
            local = urlsplit(loopback_url)
            try:
                is_loopback = ipaddress.ip_address(local.hostname or "").is_loopback
            except ValueError:
                is_loopback = False
            if (local.scheme != "http" or not is_loopback or local.username or local.password
                    or local.path not in ("", "/") or local.query or local.fragment):
                raise ValueError("loopback_url must be an HTTP loopback origin")
            endpoint = loopback_url
        self._http = httpx.AsyncClient(base_url=endpoint.rstrip("/") + "/",
            headers={"Host": url.netloc}, trust_env=False,
            auth=httpx.BasicAuth(client_id, client_secret), timeout=timeout,
            follow_redirects=False, transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        await self._http.aclose()

    async def _post(self, operation, body, *, namespace="applications"):
        if not re.fullmatch(r"[a-z][a-z0-9/-]{0,95}", operation) or "//" in operation:
            raise ValueError("Invalid application operation")
        if namespace not in ("applications", "embed"):
            raise ValueError("Invalid API namespace")
        response = await self._http.post(f"api/{namespace}/v1/{operation}", json=body)
        if not response.is_success:
            try:
                error = response.json()
                code = error.get("error", "request_failed") if isinstance(error, dict) else "request_failed"
            except ValueError:
                code = "request_failed"
            # Never include bodies, URLs or credentials in exception messages.
            if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", code):
                code = "request_failed"
            raise AurvekError(code, response.status_code)
        return response

    async def request(self, operation: str, body: dict) -> dict:
        """Call any documented JSON operation, including phone/channel APIs."""
        response = await self._post(operation, body)
        return self._json_response(response, CONTRACT)

    @staticmethod
    def _json_response(response: httpx.Response, contract: str) -> dict:
        try:
            value = response.json()
        except ValueError:
            raise AurvekError("invalid_response", response.status_code) from None
        if not isinstance(value, dict) or value.get("contract_version") != contract:
            raise AurvekError("contract_mismatch", response.status_code)
        return value

    @staticmethod
    def _interview_body(credential: str, external_project_id: str) -> dict:
        return {"delegated_credential": _bounded_string(credential, "delegated credential", 32, 256),
                "external_project_id": _bounded_string(external_project_id, "external project ID", 1, 128,
                                                        r"[A-Za-z0-9._~-]+")}

    async def _interview_request(self, operation: str, body: dict) -> dict:
        # The embed API caps JSON at 32 KiB, including multi-byte brief text.
        if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 32768:
            raise ValueError("Interview request exceeds 32 KiB")
        response = await self._post(operation, body, namespace="embed")
        return self._json_response(response, INTERVIEW_CONTRACT)

    async def ensure_interview(self, credential: str, external_project_id: str,
                               idempotency_key: str, brief: dict) -> dict:
        """Create/recover an interview; persist the key and brief before sending."""
        body = self._interview_body(credential, external_project_id)
        body.update(idempotency_key=_bounded_string(idempotency_key, "idempotency key", 16, 128,
                                                     r"[A-Za-z0-9._~-]+"),
                    brief=validate_interview_brief(brief))
        return await self._interview_request("ensure-interview", body)

    async def get_interview_state(self, credential: str, external_project_id: str) -> dict:
        """Reconcile an uncertain operation without opening another conversation."""
        return await self._interview_request("get-interview-state",
                                             self._interview_body(credential, external_project_id))

    async def update_interview_brief(self, credential: str, external_project_id: str,
                                     conversation_id: str | int, expected_revision: int, brief: dict) -> dict:
        """Apply/replay a revision; embed IDs use decimal strings on the wire."""
        if type(conversation_id) is int:
            conversation_id = str(conversation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Invalid expected revision")
        body = self._interview_body(credential, external_project_id)
        body.update(conversation_id=_bounded_string(conversation_id, "conversation ID", 1, 32, r"[0-9]+"),
                    expected_revision=expected_revision, brief=validate_interview_brief(brief))
        return await self._interview_request("update-interview-brief", body)

    async def provision(self, external_user_id: str, operation_id: str, **profile):
        return await self.request("accounts/provision", {**profile,
            "external_user_id": external_user_id, "operation_id": operation_id})

    async def account_status(self, external_user_id: str):
        return await self.request("accounts/status", {"external_user_id": external_user_id})

    async def account_session(self, external_user_id: str):
        return await self.request("accounts/session", {"external_user_id": external_user_id})

    async def update_profile(self, credential: str, external_user_id: str,
                             operation_id: str, expected_version: int, **profile):
        return await self.request("accounts/profile", {**profile,
            "delegated_credential": credential, "external_user_id": external_user_id,
            "operation_id": operation_id, "expected_version": expected_version})

    async def open_conversation(self, credential: str, operation_id: str, *,
                                context_ref=None, assistant_id=None, mode="resume",
                                conversation_id=None):
        return await self.request("open-conversation", {"delegated_credential": credential,
            "conversation": {"operation_id": operation_id, "context_ref": context_ref,
                "assistant_id": assistant_id, "mode": mode, "conversation_id": conversation_id}})

    async def bootstrap(self, credential: str, conversation_id: int, *, parent_origin: str,
                        embed_origin: str, frame_instance_id: str, ui_language="en"):
        return await self.request("issue-embed-bootstrap", {"delegated_credential": credential,
            "conversation_id": conversation_id, "parent_origin": parent_origin,
            "embed_origin": embed_origin, "frame_instance_id": frame_instance_id,
            "ui_language": ui_language})

    async def handoff(self, credential: str, operation_id: str, source_conversation_id: int,
                      assistant_id: str, **destination):
        return await self.request("handoff", {**destination, "delegated_credential": credential,
            "operation_id": operation_id, "source_conversation_id": source_conversation_id,
            "assistant_id": assistant_id})

    async def set_entry(self, credential: str, assistant_id, *, context_ref=None, channel="web"):
        return await self.request("entry-preference", {"delegated_credential": credential,
            "assistant_id": assistant_id, "context_ref": context_ref, "channel": channel})

    async def sources(self, credential: str, conversation_id: int, *, context_ref,
                      kind="messages", limit=50, cursor=None):
        return await self.request("conversation-sources", {"delegated_credential": credential,
            "conversation_id": conversation_id, "context_ref": context_ref,
            "kind": kind, "limit": limit, "cursor": cursor})

    async def source_content(self, credential: str, conversation_id: int, source_ref: str, *, context_ref):
        """Return an httpx.Response with private bytes and content metadata."""
        return await self._post("source-content", {"delegated_credential": credential,
            "conversation_id": conversation_id, "context_ref": context_ref, "source_ref": source_ref})

    async def invoke_tool(self, credential: str, conversation_id: int, name: str,
                          arguments: dict, operation_id: str, *, context_ref):
        return await self.request("invoke-tool", {"delegated_credential": credential,
            "conversation_id": conversation_id, "context_ref": context_ref,
            "name": name, "arguments": arguments, "operation_id": operation_id})
