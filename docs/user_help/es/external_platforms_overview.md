---
id: external_platforms_overview
title: "Uso de canales externos con Aurvek"
category: chat
keywords:
  - "canales externos"
  - "WhatsApp"
  - "Telegram"
  - "mensajería"
  - "llamadas telefónicas"
  - "retranscribir nota de voz"
  - "asignar conversación"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: fbdebde8c95502f19d23439c0ba69e658c5fae393958be28934480a1d778157e
---

## Respuesta breve

Puedes continuar conversaciones por WhatsApp, Telegram y teléfono. WhatsApp y Telegram son asignaciones alternativas: una conversación solo puede usar una a la vez. El teléfono es independiente y puede coexistir con cualquiera de ellas.

## Pasos

1. Busca la conversación en la barra lateral.
2. Abre su menú de tres puntos.
3. Elige **Usar para WhatsApp**, asígnala con el bot de Telegram o abre **+ > Llámame**.
4. Los mensajes y las transcripciones telefónicas se añaden a la conversación y se sincronizan con la web.
5. Para dejar de usar un canal, abre sus controles y quita la asignación.

## Notas

- Una conversación solo puede tener una app de mensajería; asignar WhatsApp quita Telegram y viceversa, sin afectar al teléfono.
- Cada canal tiene una conversación activa. Una conversación con varios canales compatibles aparece una vez en **Externos**, con varias insignias.
- WhatsApp requiere un teléfono verificado en Configuración. Telegram requiere vincular la cuenta con el bot.
- WhatsApp y Telegram admiten texto, imágenes y voz, pero no documentos de Word, hojas de cálculo u otros adjuntos.
- Si se conservó el audio original de una nota de voz, el mensaje web permite reproducirlo y usar **Retranscribir nota de voz original**. Solo se sustituye el texto tras revisar y aceptar; puede consumir saldo o créditos del proveedor.
- `!help`, `!new`, `!text`, `!voice`, `!prompt`, `!chats` y `!set` funcionan en ambas plataformas.

## Relacionado

- whatsapp_continue_conversation
- whatsapp_commands
- telegram_setup
- telegram_commands
- external_manage_conversations
- phone_calls_usage
