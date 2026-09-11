---
id: telegram_setup
title: TelegramアカウントをAurvekに連携する
category: telegram
keywords:
- Telegram
- 連携
- 接続
- 設定
- 電話番号
- ボット
prerequisites:
- 電話番号を登録済みの有効なAurvekアカウント
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 507d133ffb06746d572f3f65c20f222f1fb723facceb7bde17f08fa2d2dd96c6
---

## 要点

TelegramでAurvekボットを開き、案内に従って自分の電話番号を共有します。Aurvekアカウントの番号と一致すれば自動で連携されます。

## 手順

1. TelegramでAurvekボットを探します。ユーザー名が不明なら管理者に確認してください。
2. **開始**を押すか、任意のメッセージを送ります。
3. 電話番号を求められたら**自分の電話番号を共有**を押します。
4. Telegramの確認画面で連絡先の共有を許可します。
5. ボットがAurvekの番号を照合します。一致すると歓迎メッセージで連携完了が確認できます。
6. テキスト、音声、写真をボットへ送り、AIの返答を受け取れます。

## 注意事項

- AurvekとTelegramの電話番号は同じである必要があります。不一致なら連携できません。
- 他人の連絡先ではなく自分の番号を共有してください。ボットが送信者本人か確認します。
- 1つのTelegramは1つのAurvekアカウントにだけ連携できます。他の利用者に連携済みならエラーになります。
- 無効なAurvekアカウントは連携できません。
- 管理者が電話認証を必須にしている場合は、事前にAurvekの設定で番号を認証してください。
- 連携すると会話が自動作成され、Aurvekのウェブ版でも見られます。
- 1会話をTelegramとWhatsAppへ同時に割り当てることはできません。片方に割り当てると、もう片方が解除されます。
- 電話の割り当ては別で、そのまま併用できます。
- 連携後に`!help`で全コマンドを確認できます。

## 関連項目

- telegram_commands
- telegram_unlink
- phone_calls_usage
