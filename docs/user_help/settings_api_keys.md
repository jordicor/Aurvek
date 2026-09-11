---
id: settings_api_keys
title: Using your own API keys (BYOK)
category: settings
keywords:
  - API key
  - BYOK
  - bring your own key
  - OpenAI
  - Anthropic
  - Claude
  - Google AI
  - Gemini
  - xAI
  - Grok
  - ElevenLabs
  - MiniMax
  - Kimi
  - clave API
  - llaves API
  - credenciales
  - own keys
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

If your account allows it, you can configure your own API keys for AI providers. Go to **Settings > API Keys** tab (or `/api-credentials`), enter your keys, choose a storage mode, and save. Your keys will be used instead of the platform's default keys.

## Steps

1. Open **Settings** and click the **API Keys** tab.
2. Choose a **Storage Mode**:
   - **Session Only** -- keys are cleared when you close the browser tab.
   - **Browser Persistent** -- keys stay in your browser across sessions until you delete them.
   - **Server Storage** -- keys are encrypted and stored on the server, accessible from any device.
3. Enter your API key for one or more providers:
   - **OpenAI** -- for supported GPT, image, and voice services.
   - **Anthropic** -- for Claude models.
   - **Google AI** -- for Gemini models.
   - **xAI** -- for Grok models.
   - **MiniMax** -- for supported MiniMax models.
   - **Kimi** -- for supported Kimi models.
   - **ElevenLabs** -- for text-to-speech and voice cloning.
4. Click **Test** next to any key to verify it works before saving.
5. Click **Save All** to store all entered keys, or **Test All** to validate every key at once.
6. To remove a key, click the **X** button next to that provider, then save.
7. To remove all keys at once, use **Clear All**.

## Notes

- Your administrator sets one of four API key modes: **System Keys Only** (no BYOK), **Own Keys Only** (BYOK required), **Both (Prefer Own)**, or **Both (Prefer System)**.
- If set to "System Keys Only", the API Keys tab shows an informational message and no setup is needed.
- If own keys are required and none are configured, a warning banner appears.
- Each provider has a "Get API Key" link to its key management page.

## Related

- settings_profile
- settings_billing
