---
id: audio_input_stt
title: "Envio de mensagens de voz com conversão de voz em texto"
category: chat
keywords:
  - "voz"
  - "microfone"
  - "voz em texto"
  - "STT"
  - "entrada de áudio"
  - "gravar"
  - "ditado"
  - "retranscrever"
  - "nota de voz"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: pt
base_source_hash: e5f0b9351bfb8dc6d7d2c297e9ee4de62d51d62aee15a2132b13c582e426cc4c
---

## Resposta breve

Grave uma mensagem com o microfone; Aurvek transcreve-a e envia-a como mensagem normal. Clique no microfone para começar e depois envie ou cancele.

## Passos

1. Clique no **ícone do microfone** junto ao campo de mensagem e permita o acesso, se pedido.
2. O campo mostra temporizador e controlos de gravação; diga a mensagem.
3. Clique **enviar** para parar e transcrever, ou **cancelar** (X) para descartar.
4. O servidor coloca o texto transcrito no campo e envia-o automaticamente.

## Notas

- O navegador deve suportar MediaRecorder e ter permissão para o microfone.
- A gravação usa WebM com codec Opus.
- A transcrição consome saldo; sem saldo suficiente recebe um aviso.
- Áudio curto ou silencioso pode gerar transcrição vazia (resposta 204) e não ser enviado.
- Notas de voz do WhatsApp e Telegram usam o fluxo externo. Se o áudio original foi mantido, pode retranscrevê-lo, rever e aceitar antes de substituir o texto.

## Relacionado

- tts_listen_messages
- chat_search
- external_platforms_overview
