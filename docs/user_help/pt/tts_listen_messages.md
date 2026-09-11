---
id: tts_listen_messages
title: "Ouvir mensagens de IA com texto em voz"
category: chat
keywords:
  - "TTS"
  - "texto em voz"
  - "ouvir"
  - "áudio"
  - "reproduzir mensagem"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: pt
base_source_hash: 0f5bb5a58de885d9c9b89a64102fbab81940cd0d83c3f0ed8279e5dd773e04c7
---

## Resposta breve

Clique no altifalante de uma mensagem para converter texto em voz e transmiti-la. Se já foi reproduzida, carrega imediatamente da cache.

## Passos

1. Passe sobre a mensagem e clique no **altifalante**.
2. Vira ampulheta ao carregar e **parar** durante reprodução.
3. Clique para parar ou escolha outra mensagem, que para a atual.

## Notas

- Aurvek verifica primeiro a cache.
- Sem cache, gera e transmite por WebSocket antes de terminar todo o texto.
- Gerar TTS consome saldo; cache é gratuita.
- Sem saldo aparece aviso.
- Mensagens de utilizador e bot usam vozes conforme autor e prompt.

## Relacionado

- audio_input_stt
- chat_export_mp3
