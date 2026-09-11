---
id: whatsapp_continue_conversation
title: ウェブの会話をWhatsAppで続ける
category: whatsapp
keywords:
- WhatsApp
- 会話
- 続ける
- 割り当て
- 連携
- 外部
- メッセージ
prerequisites:
- アカウント設定で認証済みの電話番号
- ウェブチャットに既存の会話があること
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 34476ae7269e19d5479fa0e2354962262bd9fdc0f75d755c4dadf6ed72bf8413
---

## 要点

サイドバーの会話メニューで**WhatsAppで使用**を選んで割り当てます。AurvekのWhatsApp番号へ送ったメッセージが、その会話に入ります。

## 手順

1. サイドバーで続けたい会話を探します。
2. 会話の三点メニューを開きます。
3. **WhatsAppで使用**を選びます。
4. 電話番号が未設定なら、先に設定で追加するよう案内されます。
5. 会話がサイドバーの**外部**へ移り、WhatsAppアイコンが付きます。
6. 自分の電話からAurvekのWhatsApp番号へメッセージを送ると、その会話に届きます。

解除するには同じメニューで**WhatsAppから解除**を選びます。

## 注意事項

- 同時に割り当てられる会話は1つです。別の会話を割り当てると、前の割り当ては自動解除されます。
- 1会話をWhatsAppとTelegramへ同時に割り当てられません。片方へ割り当てると、もう片方は解除されます。
- 電話の割り当ては独立しており、維持できます。
- 初めてWhatsAppを使う場合は初回メッセージで会話が自動作成されます。その後ウェブから既存の会話へ割り当て直せます。
- 割り当て後は会話メニューで**テキストモード**と**音声モード**を切り替えられます。音声モードでは返答が音声になります。
- WhatsApp内でも`!chats`で一覧表示、`!set <id>`で切り替えができ、ウェブを開く必要はありません。
- メッセージはウェブ版でも同じ履歴に表示されます。
- 割り当てた会話がロックされていると新着メッセージを拒否します。`!new`で新規会話を始めてください。

## 関連項目

- whatsapp_commands
- whatsapp_setup_phone
- external_manage_conversations
- phone_calls_usage
