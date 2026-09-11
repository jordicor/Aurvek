"""Executable F0 contract examples; these do not claim runtime authorization."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from integrations.applications.models import (
    AssistantConfig, EntryPreferenceRequest, HandoffRequest, OpenConversationRequest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "application_scenarios.json"


def test_synthetic_contract_examples():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for app in data["applications"]:
        assistants = [AssistantConfig.model_validate(value) for value in app["assistants"]]
        assert app["default_assistant_id"] in {item.assistant_id for item in assistants}
    requests = data["requests"]
    OpenConversationRequest.model_validate(requests["open"])
    OpenConversationRequest.model_validate(requests["new"])
    HandoffRequest.model_validate(requests["handoff"])
    EntryPreferenceRequest.model_validate(requests["entry"])


@pytest.mark.parametrize("field,value", [("user_id", 123), ("app_id", "other"),
                                         ("prompt_id", 5), ("payer_user_id", 8),
                                         ("phone_verified", True)])
def test_requests_do_not_accept_identity_or_payer_authority(field, value):
    with pytest.raises(ValidationError):
        OpenConversationRequest.model_validate({"operation_id": "open-00001", field: value})


@pytest.mark.parametrize("model,extra", [(OpenConversationRequest, {}),
                                        (HandoffRequest, {"source_conversation_id": 1, "assistant_id": "coach"})])
def test_new_and_existing_destination_are_mutually_exclusive(model, extra):
    with pytest.raises(ValidationError):
        model.model_validate({"operation_id": "open-00001", "mode": "new", "conversation_id": 2, **extra})
