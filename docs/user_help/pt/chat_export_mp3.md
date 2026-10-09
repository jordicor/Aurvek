---
id: chat_export_mp3
title: "Exportação de uma conversa para áudio (MP3)"
category: chat
keywords:
  - "MP3"
  - "áudio"
  - "exportar"
  - "transferir"
  - "texto em voz"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-13
locale: pt
base_source_hash: b3c1ef1899fa03562f45b2774ce0b7d2121a2ed97f46e036e5ae1b9968a2ac6d
---

## Resposta breve

Pode exportar toda a conversa para MP3. Cada chamada terminada usa uma única vez a sua gravação contínua guardada, preservando ambas as vozes, pausas e falas simultâneas. Os restantes áudios guardados, incluindo notas de voz do WhatsApp/Telegram, mantêm a ordem da conversa. Só as mensagens sem áudio utilizável precisam de síntese de voz (TTS).

## Passos

1. Abra o menu de três pontos da conversa, escolha **Transferir MP3** e confirme.
2. A geração começa em segundo plano.
3. Quando terminar, abra a **Galeria multimédia** para transferir.

## Notas

- A gravação completa é usada quando a chamada pertence inteiramente a esta conversa e as suas mensagens formam um bloco consecutivo. Nos outros casos, ou se a gravação não estiver disponível, são usados os áudios individuais disponíveis das mensagens.
- Pode demorar conforme o tamanho da conversa.
- Áudio mantido conserva as vozes; mensagens sem áudio usam as vozes do prompt e da conta.
- Gravações excecionalmente longas podem usar TTS para garantir a exportação.
- O nome inclui prompt e data, por exemplo `My_Prompt_2026_03_22_14_30_00.mp3`.
- Aguarde alguns minutos se já houver geração para a mesma conversa.
- Só partes com TTS consomem saldo; reutilizar áudio não acrescenta custo TTS.

## Relacionado

- chat_export_pdf
- tts_listen_messages
