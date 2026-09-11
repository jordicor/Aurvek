---
id: telegram_commands
title: Telegramボットのコマンド
category: telegram
keywords:
- Telegram
- コマンド
- ボット
- ヘルプ
- テキスト
- 音声
- プロンプト
- 新規
- 解除
- 一覧
- 切り替え
prerequisites:
- Aurvekと連携済みのTelegramアカウント（telegram_setup参照）
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: ja
base_source_hash: 90283b72cdd9c9a04f67c402858c03345ac26776501d8a38c9552a6734298ec1
---

## 要点

AurvekのTelegramボットでは`!`で始まるコマンドで、返答の文字／音声切り替え、プロンプト変更、会話の一覧・切り替え、新規作成、連携解除ができます。全一覧は`!help`で表示します。

## 手順

1. **!help**：利用可能なコマンド一覧。
2. **!text**：返答をテキストに切り替え。
3. **!voice**：返答を読み上げ音声に切り替え。生成失敗時はテキストになります。
4. **!prompt list**：利用可能なプロンプト（AIの人格）のIDと名前を最大20件表示。
5. **!prompt \<name or id\>**：プロンプトを切り替え。IDか名前で指定でき、部分一致にも対応。
6. **!new**：新規会話を開始。前の会話は保存され、ウェブで見られます。
7. **!unlink**：Telegram連携を解除。再連携するまでボットがアカウントを認識しなくなります。
8. **!chats**：最近の会話を最大15件表示。ID、タイトル、メッセージ数、最終利用日を含み、現在の会話は`->`で示します。
9. **!set \<id\> [platform]**：会話を切り替え。`!set 1234`はTelegram、`!set 1234 whatsapp`はWhatsAppへ割り当て。省略形は`wa`、`tg`。ID先頭の`#`は任意です。

## 注意事項

- 大文字小文字は区別しません（`!Help`、`!HELP`、`!help`は同じ）。
- 通常のテキスト、音声、写真も送れます。音声は自動で文字起こしされます。
- 会話がロックされたら`!new`で新しく始めます。
- 長い返答はTelegramの文字数制限に合わせて自動分割されます。
- `!prompt`はID完全一致、名前完全一致、名前部分一致の順に照合します。

## 関連項目

- telegram_setup
- telegram_unlink
- external_manage_conversations
