---
id: limitations_file_types
title: Supported file types and upload limits
category: limitations
keywords:
  - file
  - archivo
  - upload
  - subir
  - image
  - imagen
  - PDF
  - file type
  - tipo de archivo
  - size limit
  - limite de tamano
  - supported formats
  - formatos soportados
  - attachment
  - adjunto
  - text file
  - code file
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

Aurvek accepts supported images, PDFs, and plain-text or source-code files as chat attachments. Word documents, office spreadsheets, audio, video, and archives are not accepted. Uploads must also be enabled for your account and conversation.

## Notes

- **Accepted file types.** Common images, PDFs, and plain-text formats including TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, configuration files, logs, shell scripts, and common source-code extensions.
- **Image limits.** Up to 10 images per message, under 20 MB each before automatic processing, and no more than 50 megapixels. Aurvek tries to reduce eligible images to the provider's input limit and rejects them if it cannot.
- **PDF limits.** Up to 3 PDFs per message, under 25 MB each, with a processing limit of 1,000 pages per message.
- **Text/code limits.** Up to 3 text or code files per message, under 2 MB each.
- **Combined limit.** Up to 16 attachments when combining the maximum allowed groups.
- **Clipboard paste.** You can paste images directly from your clipboard. Other clipboard content types are not supported.
- **Provider notes.** xAI (Grok) auto-converts WebP to JPEG. PDFs with GPT/xAI route through OpenRouter automatically.
- **Permission required.** File uploads are an account-level permission. If the upload button is missing, contact your administrator.
- **Mode restrictions.** File attachments are not supported in Multi-AI or GranSabio mode.

## Related

- limitations_unsupported_features
- limitations_free_models
- file_uploads
