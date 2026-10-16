"""Adapt Office documents to the chat's existing text-attachment pipeline."""

import io
import zipfile

from common import MAX_PDF_SIZE_MB, MAX_TEXT_FILE_SIZE_MB
from chat.services.attachment_uploads import AttachmentValidationError


OFFICE_DOCUMENT_TYPES = {
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
# Office XML is compressed. Bound expansion before handing it to the parser.
MAX_OFFICE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


def office_document_to_text(data: bytes, content_type: str, filename: str) -> dict:
    """Extract locally; never execute macros, formulas, or external resources.

    Keep the source extension in the derived filename so the stored Markdown
    cannot be mistaken for the original Office file. PDFs use the native PDF
    pipeline and do not pass through this adapter.
    """
    from office_oxide import Document, OfficeOxideError

    document_format = OFFICE_DOCUMENT_TYPES[content_type]
    if len(data) > MAX_PDF_SIZE_MB * 1024 * 1024:
        raise AttachmentValidationError(
            "file_size_limit", filename=filename, size=MAX_PDF_SIZE_MB,
        )
    try:
        if document_format.endswith("x"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(item.file_size for item in archive.infolist()) > MAX_OFFICE_UNCOMPRESSED_BYTES:
                    raise ValueError("Office archive exceeds expansion limit")
        with Document.from_bytes(data, document_format) as document:
            text = document.to_markdown().strip()
    except (OfficeOxideError, ValueError, zipfile.BadZipFile) as exc:
        raise AttachmentValidationError("upload_invalid") from exc
    if not text:
        raise AttachmentValidationError("upload_invalid")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_SIZE_MB * 1024 * 1024:
        raise AttachmentValidationError(
            "file_size_limit", filename=filename + ".md", size=MAX_TEXT_FILE_SIZE_MB,
        )
    return {"data": encoded, "content_type": "text/markdown", "filename": filename + ".md"}
