---
id: limitations_file_types
title: "Tipos de archivo compatibles y límites de subida"
category: limitations
keywords:
  - "archivo"
  - "subir"
  - "imagen"
  - "PDF"
  - "tipo de archivo"
  - "límite de tamaño"
  - "formatos compatibles"
  - "adjunto"
  - "texto"
  - "código"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## Respuesta breve

Aurvek acepta como adjuntos imágenes, PDF y archivos de texto o código compatibles. No acepta documentos Word, hojas de cálculo ofimáticas, audio, vídeo ni archivos comprimidos. Las subidas también deben estar activadas para tu cuenta y conversación.

## Notas

- **Tipos admitidos:** imágenes comunes, PDF y texto plano como TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, configuración, registros, scripts de shell y extensiones de código habituales.
- **Imágenes:** hasta 10 por mensaje, menos de 20 MB cada una antes del procesamiento y no más de 50 megapíxeles. Aurvek intenta reducirlas al límite del proveedor y las rechaza si no puede.
- **PDF:** hasta 3 por mensaje, menos de 25 MB cada uno y 1.000 páginas procesadas por mensaje.
- **Texto/código:** hasta 3 archivos por mensaje, de menos de 2 MB cada uno.
- **Total:** hasta 16 adjuntos al combinar los máximos.
- Puedes pegar imágenes del portapapeles, pero no otros tipos.
- xAI (Grok) convierte WebP a JPEG. Los PDF con GPT/xAI se enrutan automáticamente por OpenRouter.
- La subida es un permiso de cuenta; si falta el botón, contacta con el administrador.
- Multi-IA y GranSabio no admiten archivos.

## Relacionado

- limitations_unsupported_features
- limitations_free_models
- file_uploads
