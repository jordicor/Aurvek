---
id: web_search_modes
title: ウェブ検索モードの違い
category: search
keywords:
- 検索
- ネイティブ
- Perplexity
- モード
- 検索エンジン
- 設定
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: ja
base_source_hash: 632298e8e9d58cedacdccbd71347370159ab69fa6929922203f4a729c8c9b12d
---

## 要点

Aurvekの検索には**ネイティブ**と**Perplexity**があります。ネイティブはモデル内蔵の検索で、通常はこちらがおすすめです。Perplexityは外部検索サービスを使います。アカウント設定で切り替えられます。

## 手順

1. プロフィールアイコンから**設定**を開きます。
2. **ウェブ検索**欄を探します。
3. エンジンを選びます。
   - **ネイティブ**（推奨）：モデル内蔵検索で、速く会話の文脈に統合されています。
   - **Perplexity**：別サービスのPerplexity AI（sonar-pro）へ検索を送ります。サーバーに未設定なら選択できません。
4. プロフィールを保存すると、次のメッセージから反映されます。

## 注意事項

- ネイティブ検索はClaude、GPT、xAIで利用できます。Geminiなど非対応モデルでは自動でPerplexityに切り替わります。
- ネイティブではモデル自身が検索し、結果を回答へ直接組み込みます。文中に引用が付くこともあります。
- PerplexityではAIがツールとして検索を依頼し、返された結果から自分の回答を作る2段階処理です。
- 設定は全会話に適用されます。ただしプロンプトごとに検索オン／オフを強制できます。

## 関連項目

- web_search_usage
