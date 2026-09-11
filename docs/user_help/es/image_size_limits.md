---
id: image_size_limits
title: "Por qué las imágenes enviadas a la IA se reducen a 1568 píxeles"
category: limitations
keywords:
  - "tamaño de imagen"
  - "calidad de imagen"
  - "resolución"
  - "imagen reducida"
  - "imagen borrosa"
  - "captura de pantalla"
  - "1568"
  - "límites de proveedor"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
locale: es
base_source_hash: 398c00ba92296bf81e63380a1cecb2b34709488fea96970c4ba964726d3e7eba
---

## Respuesta breve

Al adjuntar una imagen, Aurvek reduce su lado más largo a un máximo de 1568 píxeles antes de enviarla a la IA. Mantiene la proporción y no recorta píxeles. Solo afecta a imágenes de mensajes; no a fotos de perfil, avatares de prompts ni fondos de tema.

## Notas

- En capturas con texto pequeño, recorta la zona concreta. Varias capturas centradas suelen funcionar mejor que una enorme.
- Aurvek guarda y muestra la versión reducida, de 1568 px o menos. Tras guardarla, no conserva los bytes originales a resolución completa.
- El límite solo afecta a imágenes subidas, no a las generadas por la IA con DALL-E, Ideogram, Gemini u otros.

## Relacionado

- file_uploads
- image_generation
- limitations_file_types
