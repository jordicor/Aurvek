---
id: telegram_unlink
title: TelegramとAurvekの連携を解除する
category: telegram
keywords:
- Telegram
- 連携解除
- 切断
- 削除
- 解除
prerequisites:
- 現在Aurvekと連携しているTelegramアカウント（telegram_setup参照）
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: ja
base_source_hash: 856229a852405b67ff49f83d0bfc50d35d10122514120cb535d0884030d4a2a5
---

## 要点

TelegramのAurvekボットへ`!unlink`を送ると、TelegramとAurvekアカウントの連携がすぐ解除されます。

## 手順

1. TelegramでAurvekボットとの会話を開きます。
2. `!unlink`を入力して送信します。
3. 「Telegramとアカウントの連携を解除しました」と通知されます。
4. ボットはアカウントを認識しなくなり、その後に送ると再連携を案内します。

## 注意事項

- Aurvekアカウントや会話は削除されません。ウェブ版で引き続き履歴を見られます。
- Telegram内のチャットも削除されず、メッセージは残ります。ただしボットは連携済み利用者として応答しなくなります。
- 再連携するには任意のメッセージを送り、電話番号の共有手順を繰り返します（telegram_setup参照）。
- 現在、ウェブ版からは解除できず、ボットから行います。
- 管理者は管理画面で利用者の`telegram_chat_id`を空にして解除できます。

## 関連項目

- telegram_setup
- telegram_commands
