"""Operator admission prepares real native accounts without widening linked users."""
import json

import pytest
from pydantic import ValidationError

from integrations.applications.accounts import (
    AccountPolicy, ApplicationAccountService, LinkAccountRequest,
    MembershipUpdateRequest, ProvisionAccountRequest,
)
from integrations.applications.authentication import issue_account_session
from integrations.applications.models import OpenConversationRequest
from integrations.embed.models import EmbedError
from tests.test_application_frames import application_frames
from tests.test_embed_native_creation import native_pilot


def test_capabilities_are_operator_only_strict_and_default_to_text():
    assert AccountPolicy().capabilities == {"text": True}
    with pytest.raises(ValidationError):
        AccountPolicy(capabilities={"attachments": "true"})
    with pytest.raises(ValidationError):
        ProvisionAccountRequest(operation_id="provision-0001", external_user_id="new", capabilities={"attachments": True})


@pytest.mark.asyncio
async def test_new_account_and_default_context_follow_policy_but_linked_flags_stay_native(application_frames):
    pilot, applications, principal, first, _, _ = application_frames
    service = ApplicationAccountService(pilot.store)
    caps = {"text": True, "attachments": True, "image_generation": True}
    async with pilot.connect() as connection:
        await connection.execute("UPDATE EMBED_APPS SET config_json=json_set(config_json,'$.capabilities',json(?)) WHERE app_id='first'", (json.dumps(caps),))
        await connection.execute("UPDATE APPLICATION_ASSISTANTS SET config_json=json_set(config_json,'$.capabilities',json(?)) WHERE app_id='first'", (json.dumps(caps),))
        await connection.execute("UPDATE USER_DETAILS SET allow_file_upload=0,allow_image_generation=0 WHERE user_id=1")
        await connection.commit()
    policy = AccountPolicy(allow_provision=True, allow_link=True, allow_membership_management=True,
        admit_new_accounts=True, llm_id=7, assistant_ids=["default"], capabilities={**caps, "video_generation": True})
    await service.configure_policy("first", policy)
    created = await service.provision("first", ProvisionAccountRequest(operation_id="new-capability-account", external_user_id="new"))
    binding = await service.account_binding("first", "new")
    async with pilot.connect() as connection:
        flags = await (await connection.execute("SELECT allow_file_upload,allow_image_generation FROM USER_DETAILS WHERE user_id=?", (binding["user_id"],))).fetchone()
        member = await (await connection.execute("SELECT capabilities_json FROM EMBED_MEMBERSHIPS WHERE app_id='first' AND subject=?", (created["subject"],))).fetchone()
    assert tuple(flags) == (1, 1) and json.loads(member[0]) == caps
    session = await issue_account_session(pilot.store, "first", "new")
    identity = await pilot.store.inspect_identity("first", session["delegated_credential"])
    opened = await applications.open_conversation(identity, OpenConversationRequest(operation_id="open-new-default-context"))
    assert opened["capabilities"]["attachments"] and opened["capabilities"]["image_generation"]
    assert not opened["capabilities"].get("video_generation")
    async with pilot.connect() as connection:
        old_context = await (await connection.execute("SELECT capabilities_json FROM APPLICATION_PARTICIPANTS WHERE context_id=?", (first["context_id"],))).fetchone()
    assert json.loads(old_context[0]) == {"text": True}

    linked = await service.link(principal, LinkAccountRequest(operation_id="link-capability-account", external_user_id="linked"))
    async with pilot.connect() as connection:
        native = await (await connection.execute("SELECT allow_file_upload,allow_image_generation FROM USER_DETAILS WHERE user_id=1")).fetchone()
    assert tuple(native) == (0, 0)
    linked_session = await issue_account_session(pilot.store, "first", "linked")
    await service.configure_policy("first", policy)
    active = await service.set_membership("first", MembershipUpdateRequest(operation_id="keep-capability-membership",
        external_user_id="linked", expected_version=linked["membership_version"], active=True))
    assert active["membership_version"] == linked["membership_version"]
    assert await pilot.store.inspect_identity("first", linked_session["delegated_credential"])
    assert await pilot.store.inspect_identity("first", session["delegated_credential"])

    removed = await service.set_membership("first", MembershipUpdateRequest(operation_id="suspend-capability-member",
        external_user_id="linked", expected_version=active["membership_version"], active=False))
    reduced = policy.model_copy(update={"capabilities": {"text": True, "image_generation": True}})
    await service.configure_policy("first", reduced)
    with pytest.raises(EmbedError):
        await pilot.store.inspect_identity("first", session["delegated_credential"])
    restored = await service.set_membership("first", MembershipUpdateRequest(operation_id="readmit-capability-member",
        external_user_id="linked", expected_version=removed["membership_version"], active=True))
    assert restored["membership_version"] == removed["membership_version"] + 1
    async with pilot.connect() as connection:
        member = await (await connection.execute("SELECT capabilities_json FROM EMBED_MEMBERSHIPS WHERE app_id='first' AND subject=?", (linked["subject"],))).fetchone()
        flags = await (await connection.execute("SELECT allow_file_upload,allow_image_generation FROM USER_DETAILS WHERE user_id=1")).fetchone()
    assert json.loads(member[0]) == reduced.capabilities and tuple(flags) == (0, 0)
