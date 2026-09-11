---
id: limitations_free_models
title: 支払いなしで使えるAIモデルと機能
category: limitations
keywords:
- 無料
- モデル
- 料金
- 残高
- 費用
- 無料枠
- BYOK
- APIキー
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: ja
base_source_hash: e07fb134f303ba395594d4a999c040d3aa76e917cac7c0580451e979f19b099a
---

## 要点

Aurvekは固定のサブスクリプション区分ではなく残高制です。AIメッセージごとに少額が残高から引かれます。利用モデルは会話相手のプロンプト（AIアシスタント）とアカウント権限で決まり、別のモデル一覧を持つ「無料枠」はありません。作成者が新規ユーザー向け初期残高を設定していれば、登録後すぐ入金せずに会話できます。

## 注意事項

- **残高制**：モデルと使用トークンに応じて課金されます。安価なモデルは1セント未満、高性能モデルはより高額です。
- **初期残高**：一部のプロンプトは、その紹介ページから登録した新規ユーザーに最大$5を提供し、無料で開始できます。
- **BYOK**：許可されていればOpenAI、Anthropic、Google、xAIの自分のAPIキーを使い、プロバイダーへ直接支払います。プラットフォームのAPI費用はかかりません。VIPプロンプトでは作成者の上乗せ分のため少額残高（$0.10）が必要です。
- **利用モデル**：作成者がモデルを固定したり選択肢を絞ったりできます。プロバイダーはOpenAI（GPT）、Anthropic（Claude）、Google（Gemini）、xAI（Grok）、OpenRouterです。
- **機能**：添付、画像生成、TTS、音声通話は管理者が設定するアカウント権限で、支払い状況には連動しません。

## 関連項目

- limitations_file_types
