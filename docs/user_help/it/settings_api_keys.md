---
id: settings_api_keys
title: "Usare le proprie chiavi API (BYOK)"
category: settings
keywords:
  - "chiave API"
  - "BYOK"
  - "usa la tua chiave"
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
  - "credenziali"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: it
base_source_hash: 8d6ae4ed5c1d21ca604207d455d6d0683c5f58383e241db56f859b1f39e29dd1
---

## Risposta breve

Se l’account lo consente, configura le chiavi in **Impostazioni > Chiavi API** o `/api-credentials`, scegli l’archiviazione e salva. Verranno usate al posto delle chiavi predefinite secondo la modalità dell’account.

## Passaggi

1. Apri **Chiavi API** e scegli **Solo questa sessione** (cancellate chiudendo la scheda), **Questo browser** (fino alla rimozione) o **Server sicuro** (crittografate e disponibili su ogni dispositivo).
2. Inserisci chiavi per OpenAI (GPT, immagini, voce), Anthropic (Claude), Google AI (Gemini), xAI (Grok), MiniMax, Kimi o ElevenLabs (TTS e clonazione vocale).
3. Usa **Test**, **Verifica tutto**, quindi **Salva tutto**.
4. Per rimuovere, premi X accanto al provider e salva, oppure **Cancella tutto**.

## Note

- L’amministratore sceglie: **Solo chiavi di sistema**, **Solo chiavi personali**, **Entrambe (preferisci personali)** o **Entrambe (preferisci sistema)**.
- Con sole chiavi di sistema appare un messaggio e non serve configurare. Se le chiavi personali sono obbligatorie ma assenti, compare un avviso.
- Ogni provider ha un link **Ottieni chiave API**.

## Articoli correlati

- settings_profile
- settings_billing
