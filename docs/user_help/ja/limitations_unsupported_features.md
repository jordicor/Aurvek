---
id: limitations_unsupported_features
title: Aurvekが現在対応していない機能
category: limitations
keywords:
- 非対応
- 未対応
- 機能
- モバイルアプリ
- オフライン
- セルフホスト
- 制限
- 要望
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 0578e1be6ca4f0e74d28e52e4d7547c9ac190cbc9c663c970ce79f92c65cea88
---

## 要点

Aurvekはブラウザで使うウェブサービスです。他のAIサービスで見られるネイティブモバイルアプリ、オフライン利用、一般利用者向けセルフホストなどは現在提供していません。

## 注意事項

- **モバイルアプリなし**：iOS／Androidアプリはありません。スマートフォンのブラウザで画面サイズに対応したウェブ版を使ってください。
- **オフライン不可**：AIとのやり取りにはインターネット接続が必要です。
- **一般利用者向けセルフホストなし**：ホスト型サービスです。GitHubリポジトリは自分の環境に展開する開発者向けです。
- **Multi-AI／GranSabioの添付不可**：テキストのみ対応します。
- **WhatsApp／Telegramの文書添付不可**：画像、音声、テキストに対応しますが、文書ファイルは扱えません。
- **動画アップロード不可**：AIで動画生成はできますが、解析用に動画ファイルを添付できません。
- **リアルタイム共同作業なし**：各会話は1つのアカウントに属します。
- **一般公開の開発者APIなし**：有効な場合に使える内蔵チャネルはWhatsApp、Telegram、電話です。
- **シークレットの外部割り当て不可**：メッセージ連携や電話チャネルには割り当てられません。

## 関連項目

- limitations_file_types
- limitations_free_models
- phone_calls_usage
