---
id: chat_search
title: "Búsqueda de mensajes entre conversaciones"
category: chat
keywords:
  - "buscar"
  - "encontrar"
  - "búsqueda de mensajes"
  - "encontrar mensaje"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: es
base_source_hash: 198623651c421431e94256968eead8a24cb97157d3f9c5cef4b7228e10355af2
---

## Respuesta breve

La barra de la barra lateral busca en todas tus conversaciones. Muestra fragmentos coincidentes resaltados y agrupados por conversación; al pulsar un resultado, saltas al mensaje.

## Pasos

1. Escribe al menos 3 caracteres en el campo de búsqueda.
2. Pulsa **Enter**.
3. Los resultados sustituyen la lista normal y muestran conversación, tipo de mensaje, fecha y fragmento resaltado.
4. Pulsa un resultado para abrir la conversación y desplazarte al mensaje.
5. Usa **Cargar más** si hay más resultados.
6. Pulsa **X** o borra el texto para salir de la búsqueda.

## Notas

- Usa búsqueda de texto completo FTS5 y admite frases exactas entre comillas, por ejemplo `"visa interview"`.
- Ordena por relevancia y carga lotes de 30.
- Puedes quitar el resaltado en el chat con su pequeña **x**.
- Solo busca en tus propias conversaciones.

## Relacionado

- chat_bookmarks
- chat_folders
