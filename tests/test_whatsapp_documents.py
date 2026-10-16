"""Document ingress uses the same PDF/text attachment contract as web chat."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from chat.services import office_documents
from chat.services.attachment_uploads import AttachmentValidationError, create_pending_attachment_from_upload
import file_storage
from integrations.embed.models import EmbedError
from integrations.whatsapp import routes
from i18n import Translator


FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp_documents"
MIMES = {extension: mime for mime, extension in office_documents.OFFICE_DOCUMENT_TYPES.items()}
MIMES["pdf"] = "application/pdf"


@pytest.mark.parametrize("filename", ["report.docx", "sales.xlsx", "report.pptx"])
def test_office_text_keeps_document_content_and_tables(filename):
    extension = filename.rsplit(".", 1)[1]
    converted = office_documents.office_document_to_text(
        (FIXTURES / filename).read_bytes(), MIMES[extension], filename,
    )
    text = converted["data"].decode("utf-8")
    assert "Aurvek" in text and "42" in text and "violet-73" in text
    assert converted["filename"] == filename + ".md"
    assert converted["content_type"] == "text/markdown"
    if extension == "xlsx":
        assert "Sales" in text and "Notes" in text and "84" in text


@pytest.mark.parametrize("extension,expected", [
    ("doc", "Word 97-SR2"), ("xls", "replaceMe"), ("ppt", "This is page two"),
])
def test_legacy_office_documents(extension, expected):
    converted = office_documents.office_document_to_text(
        (FIXTURES / f"legacy.{extension}").read_bytes(), MIMES[extension], f"legacy.{extension}",
    )
    assert expected in converted["data"].decode("utf-8")


def test_invalid_and_expanded_office_files_are_rejected(monkeypatch):
    with pytest.raises(AttachmentValidationError):
        office_documents.office_document_to_text(b"not a document", MIMES["docx"], "bad.docx")
    monkeypatch.setattr(office_documents, "MAX_OFFICE_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(AttachmentValidationError):
        office_documents.office_document_to_text(
            (FIXTURES / "report.docx").read_bytes(), MIMES["docx"], "large.docx",
        )


def test_extracted_text_limit_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(office_documents, "MAX_TEXT_FILE_SIZE_MB", 0)
    with pytest.raises(AttachmentValidationError) as error:
        office_documents.office_document_to_text(
            (FIXTURES / "report.docx").read_bytes(), MIMES["docx"], "large.docx",
        )
    assert error.value.code == "file_size_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["report.pdf", "report.docx", "sales.xlsx", "report.pptx"])
async def test_document_adapter_uses_web_storage_with_owner_scope(mock_db, tmp_path, monkeypatch, filename):
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "blobs")
    async with mock_db() as conn:
        await conn.execute("INSERT INTO USERS(id, username) VALUES(7, 'synthetic')")
        await conn.execute("INSERT INTO CONVERSATIONS(id, user_id) VALUES(11, 7)")
        await conn.commit()
    extension = filename.rsplit(".", 1)[1]
    attachment = {"data": (FIXTURES / filename).read_bytes(), "filename": filename, "content_type": MIMES[extension]}
    if extension != "pdf":
        attachment = office_documents.office_document_to_text(**attachment)
    pending = await create_pending_attachment_from_upload(user_id=7, conversation_id=11, **attachment)
    result = await file_storage.read_pending_attachment_bytes(pending.public_id, user_id=7, conversation_id=11)
    assert result is not None and result[0] == attachment["data"]
    assert pending.kind == ("pdf" if extension == "pdf" else "text")
    assert await file_storage.read_pending_attachment_bytes(pending.public_id, user_id=8, conversation_id=11) is None


@pytest.mark.asyncio
async def test_invalid_pdf_is_rejected_by_existing_web_validator():
    with pytest.raises(ValueError):
        await create_pending_attachment_from_upload(
            user_id=7, conversation_id=11, data=b"not a PDF", filename="bad.pdf", content_type="application/pdf",
        )


@pytest.fixture
def document_ingress(monkeypatch):
    user = SimpleNamespace(id=7, username="synthetic", is_enabled=True, can_send_files=True, ui_language="es")
    admission = SimpleNamespace(
        scope=SimpleNamespace(user_id=7, conversation_id=11), response_mode="text", as_dict=lambda: {},
    )
    state = SimpleNamespace(admission=admission, operation=None, attach=lambda context: context)
    foreground = SimpleNamespace(
        context={}, decision=SimpleNamespace(phone_active=False), is_current=AsyncMock(return_value=True),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(0,)))),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def connection(**kwargs):
        yield db

    async def response_stream():
        yield 'data: {"content":"Received","message_ids":{"user":1,"bot":2}}\n\n'

    async def serialized(_user_id, awaitable):
        return await awaitable

    process = AsyncMock(side_effect=lambda **kwargs: StreamingResponse(response_stream()))
    download = AsyncMock()
    monkeypatch.setattr(routes, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(routes, "application_command", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "check_messaging", AsyncMock())
    monkeypatch.setattr(routes, "get_db_connection", connection)
    monkeypatch.setattr(routes, "capture_non_phone_channel_turn", AsyncMock(return_value=foreground))
    monkeypatch.setattr(routes, "attach_message_channel_provenance", lambda context, metadata: {"provenance": metadata})
    monkeypatch.setattr(routes, "process_save_message", process)
    monkeypatch.setattr(routes, "serialize_user_billing_response", serialized)
    monkeypatch.setattr(routes, "download_external_media", download)
    transport = SimpleNamespace(send_message=AsyncMock())

    async def receive(mime, body=""):
        return await routes._process_whatsapp_message(
            SimpleNamespace(headers={}),
            {"From": "whatsapp:+15550007701", "To": "whatsapp:+15550007702", "MessageSid": "SM-synthetic",
             "Body": body, "MediaUrl0": "https://api.twilio.com/media/synthetic", "MediaContentType0": mime},
            application_state=state, twilio_client=transport,
        )

    return SimpleNamespace(user=user, download=download, process=process, transport=transport, receive=receive, db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["report.pdf", "report.docx", "sales.xlsx", "report.pptx"])
@pytest.mark.parametrize("caption", ["", "Explain this document"])
async def test_whatsapp_documents_reach_shared_chat_runtime(document_ingress, filename, caption):
    flow = document_ingress
    original = (FIXTURES / filename).read_bytes()
    flow.download.return_value = original
    extension = filename.rsplit(".", 1)[1]
    result = await flow.receive(MIMES[extension].upper() + "; charset=binary", caption)
    assert result == {"status": "success"}
    flow.process.assert_awaited_once()
    request = flow.process.await_args.kwargs
    assert request["text_plain"] == caption
    attachment = request["files"][0]
    if extension == "pdf":
        assert attachment["content_type"] == "application/pdf" and attachment["data"] == original
    else:
        assert attachment["filename"].endswith(f".{extension}.md")
        assert "violet-73" in attachment["data"].decode("utf-8")
    assert request["channel_context"]["provenance"]["content_kind"] == ("mixed" if caption else "document")
    assert flow.db.execute.await_args_list[0].args[1][2] == "document"
    flow.download.assert_awaited_once_with(
        "https://api.twilio.com/media/synthetic", max_bytes=16 * 1024 * 1024, total_timeout=60,
    )
    assert flow.transport.send_message.await_args.kwargs["body"] == "Received"


@pytest.mark.asyncio
async def test_file_permission_blocks_document_download(document_ingress):
    flow = document_ingress
    flow.user.can_send_files = False
    await flow.receive(MIMES["pdf"])
    flow.download.assert_not_awaited()
    flow.process.assert_not_awaited()
    assert flow.transport.send_message.await_args.kwargs["body"] == Translator("es").t("chat_errors.file_uploads_disabled")


@pytest.mark.asyncio
async def test_application_capability_blocks_document_download(document_ingress, monkeypatch):
    flow = document_ingress
    monkeypatch.setattr(routes, "check_messaging", AsyncMock(side_effect=EmbedError("application_capability_denied", 403)))
    assert await flow.receive(MIMES["pdf"]) == {"status": "denied"}
    flow.download.assert_not_awaited()
    flow.process.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["size", "corrupt"])
async def test_document_failure_does_not_send_caption_without_attachment(document_ingress, failure):
    flow = document_ingress
    if failure == "size":
        flow.download.side_effect = HTTPException(413, "private download details")
    else:
        flow.download.return_value = b"not a Word document"
    await flow.receive(MIMES["docx"], "Explain this document")
    flow.process.assert_not_awaited()
    assert "private" not in flow.transport.send_message.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_unknown_media_stays_rejected(document_ingress):
    flow = document_ingress
    await flow.receive("application/x-msdownload")
    flow.download.assert_not_awaited()
    flow.process.assert_not_awaited()
