---
id: settings_api_keys
title: "Uso de tus propias claves de API (BYOK)"
category: settings
keywords:
  - "clave de API"
  - "BYOK"
  - "clave propia"
  - "OpenAI"
  - "Anthropic"
  - "Claude"
  - "Google AI"
  - "Gemini"
  - "xAI"
  - "Grok"
  - "ElevenLabs"
  - "MiniMax"
  - "Kimi"
  - "credenciales"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: 8d6ae4ed5c1d21ca604207d455d6d0683c5f58383e241db56f859b1f39e29dd1
---

## Respuesta breve

Si tu cuenta lo permite, configura tus claves en **Configuración > Claves de API** o `/api-credentials`, elige cómo guardarlas y pulsa guardar. Sustituirán a las claves predeterminadas de la plataforma.

## Pasos

1. Abre **Configuración > Claves de API**.
2. Elige **Modo de almacenamiento**: **Solo sesión** (hasta cerrar la pestaña), **Persistente en el navegador** (hasta borrarlas) o **En el servidor** (cifradas y disponibles en otros dispositivos).
3. Introduce claves para OpenAI (GPT, imagen y voz), Anthropic (Claude), Google AI (Gemini), xAI (Grok), MiniMax, Kimi o ElevenLabs (TTS y clonación de voz).
4. Pulsa **Probar** junto a una clave.
5. Usa **Guardar todo** o **Probar todo**.
6. Para quitar una, pulsa **X** y guarda; para todas, **Borrar todo**.

## Notas

- El administrador elige entre **Solo claves del sistema**, **Solo claves propias**, **Ambas (preferir propias)** o **Ambas (preferir sistema)**.
- Con solo claves del sistema, la pestaña es informativa y no requiere configuración.
- Si se exigen claves propias y faltan, aparece un aviso.
- Cada proveedor incluye un enlace **Obtener clave de API**.

## Relacionado

- settings_profile
- settings_billing
