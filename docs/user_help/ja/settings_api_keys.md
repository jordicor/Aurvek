---
id: settings_api_keys
title: 自分のAPIキーを使う（BYOK）
category: settings
keywords:
- APIキー
- BYOK
- 設定
- 認証情報
- OpenAI
- Anthropic
- Claude
- Google AI
- Gemini
- xAI
- Grok
- ElevenLabs
- MiniMax
- Kimi
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 8d6ae4ed5c1d21ca604207d455d6d0683c5f58383e241db56f859b1f39e29dd1
---

## 要点

許可されたアカウントでは、自分のAIプロバイダーのAPIキーを設定できます。**設定 > APIキー**（または`/api-credentials`）でキーと保存方式を指定して保存すると、プラットフォームの標準キーに代わって使われます。

## 手順

1. **設定**の**APIキー**タブを開きます。
2. **保存モード**を選びます。
   - **セッションのみ**：ブラウザタブを閉じると消えます。
   - **このブラウザー**：削除するまで次回以降もブラウザに残ります。
   - **安全なサーバー**：暗号化して保存し、どの端末からも使えます。
3. 1つ以上のプロバイダーのキーを入力します。
   - **OpenAI**：対応GPT、画像、音声サービス。
   - **Anthropic**：Claude。
   - **Google AI**：Gemini。
   - **xAI**：Grok。
   - **MiniMax**／**Kimi**：各社の対応モデル。
   - **ElevenLabs**：読み上げと声のクローン。
4. 各キー横の**テスト**で保存前に確認できます。
5. **すべて保存**で保存し、**すべてテスト**で一括検証できます。
6. キーの削除はプロバイダー横の**X**を押して保存します。
7. 一括削除は**すべて消去**を使います。

## 注意事項

- 管理者が**システムキーのみ**（BYOK不可）、**自分のキーのみ**（必須）、**両方・自分を優先**、**両方・システムを優先**のいずれかを設定します。
- システムキーのみなら案内が表示され、設定は不要です。
- 自分のキーが必須なのに未設定の場合、警告バナーが出ます。
- 各プロバイダーの**APIキーを取得**リンクからキー管理画面を開けます。

## 関連項目

- settings_profile
- settings_billing
