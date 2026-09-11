---
id: external_platforms_overview
title: Aurvekを外部チャネルで利用する
category: chat
keywords:
- 外部
- WhatsApp
- Telegram
- 電話
- 音声
- 再文字起こし
- 割り当て
- 連携
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: fbdebde8c95502f19d23439c0ba69e658c5fae393958be28934480a1d778157e
---

## 要点

Aurvekの会話をWhatsApp、Telegram、電話で続けられます。1会話のメッセージ連携はWhatsAppかTelegramのどちらか1つです。電話の割り当ては独立しており、どちらとも併用できます。

## 手順

1. サイドバーで外部利用する会話を探します。
2. 会話の三点メニューを開きます。
3. **WhatsAppで使用**を選ぶか、Telegramボットから割り当てるか、電話用に**+ > 電話をかける**を開きます。
4. メッセージと電話の文字起こしが割り当てた会話に追加され、ウェブと同期します。
5. チャネルをやめるには、その操作画面で割り当てを解除します。

## 注意事項

- WhatsAppを割り当てるとその会話のTelegram割り当てが解除され、逆も同様です。電話の割り当ては残ります。
- WhatsApp、Telegram、電話の各チャネルで有効な会話は1つです。複数チャネルを併用する会話は**外部**に1回表示され、複数のバッジが付きます。
- WhatsAppには設定で認証済みの電話番号が必要です。
- Telegramは事前にAurvekボット経由で連携します（telegram_setup参照）。
- 両メッセージサービスはテキスト、画像、音声に対応しますが、Wordや表計算などの文書添付には非対応です。
- WhatsApp／Telegram音声メモの原音が保存されていれば、ウェブ上で再生し、**元の音声メモを再文字起こし**できます。結果を確認・承認してから置き換えます。残高やプロバイダークレジットを消費する場合があります。
- `!help`、`!new`、`!text`、`!voice`、`!prompt`、`!chats`、`!set`などは両方で使えます。

## 関連項目

- whatsapp_continue_conversation
- whatsapp_commands
- telegram_setup
- telegram_commands
- external_manage_conversations
- phone_calls_usage
