---
id: external_manage_conversations
title: WhatsAppやTelegramから会話を管理する
category: chat
keywords:
- 会話
- 一覧
- 切り替え
- 割り当て
- WhatsApp
- Telegram
- コマンド
prerequisites:
- Aurvekと連携済みのWhatsAppまたはTelegramアカウント
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 5b8b56741a26a97b05c721945c2e88b9d8ecb91eb691ee1bddc106b8d5e9da65
---

## 要点

WhatsAppやTelegramから`!chats`と`!set`で会話を一覧表示・切り替えできます。電話の割り当ては別管理で、これらのコマンドでは変わりません。

## 手順

1. `!chats`を送ると最近の会話が表示されます。各行にID、タイトル、メッセージ数、最終利用日、割り当て先が表示され、現在の会話は`->`で示されます。
2. `!set <id>`（例：`!set 1234`）で、その会話を送信元のプラットフォームに割り当てます。
3. WhatsAppから`!set 1234 telegram`、Telegramから`!set 1234 whatsapp`を送ると、反対側に移せます。省略形`tg`と`wa`も使えます。
4. そのプラットフォームでの次のメッセージは選択した会話へ入ります。前の会話は削除されず、割り当てのみ解除されます。

## 注意事項

- 各プラットフォームで有効な会話は同時に1つです。
- 1会話のメッセージ連携はWhatsAppかTelegramのどちらか1つです。独立した電話割り当ては併用できます。
- 使用中のプラットフォームから会話を移すと、次のメッセージで新しい会話が自動作成されると通知されます。
- ロックされた会話は割り当て不可です。`!new`で新しい会話を始めてください。
- 別のプラットフォームへの割り当てには、移動先とのアカウント連携が必要です。
- ウェブのサイドバーからも割り当てられます。
- 電話チャネルの割り当て・管理はウェブチャットの**+ > 電話をかける**で行います。

## 関連項目

- whatsapp_commands
- telegram_commands
- whatsapp_continue_conversation
- external_platforms_overview
- phone_calls_usage
