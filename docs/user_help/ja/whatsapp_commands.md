---
id: whatsapp_commands
title: WhatsAppで使えるコマンド
category: whatsapp
keywords:
- WhatsApp
- コマンド
- ヘルプ
- テキスト
- 音声
- プロンプト
- 新規
- 一覧
- 切り替え
prerequisites:
- アカウントに割り当て済みの有効なWhatsApp会話
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
locale: ja
base_source_hash: a88730b3fdf5473fa22b95fecca28ab8a93e10bf1b964484b89d9d9bd31db2df
---

## 要点

WhatsAppではチャットを離れずにコマンドで会話を操作できます。すべて`!`で始まり、大文字小文字は区別しません。

## 手順

1. **!help**：利用できるコマンド一覧を表示します。
2. **!text**：AIの返答をテキストにします。`text mode`、`text_mode`も使えます。
3. **!voice**：音声で返答します。`voice mode`、`voice_mode`も使えます。
4. **!prompt list**：利用可能なプロンプトをIDと名前付きで最大20件表示します。
5. **!prompt \<name or id\>**：現在の会話のプロンプトを変更します。数値IDまたは名前（部分一致可）を使います。例：`!prompt 42`、`!prompt email campaigns`。
6. **!new**：新しい会話を開始します。前の会話は保存され、ウェブ版で見られます。
7. **!chats**：最近の会話のID、タイトル、メッセージ数、最終利用日、プラットフォームバッジを表示します。現在のWhatsApp会話は`->`で示します。
8. **!set \<id\> [platform]**：`!set 1234`で会話#1234をWhatsAppへ、`!set 1234 telegram`でTelegramへ割り当てます。`wa`と`tg`の省略形も使え、IDの`#`は任意です。

## 注意事項

- コマンドはAIへ渡る前に処理され、AIへの入力にはなりません。
- `!prompt`はID完全一致、名前完全一致（大文字小文字を区別せず）、名前部分一致の順に照合します。
- 会話がロックされると通常メッセージは拒否されます。`!new`で新しく始めてください。
- 音声モードはTTSで音声を生成し、プランによって追加残高を消費する場合があります。

## 関連項目

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
