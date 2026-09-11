---
id: limitations_file_types
title: 対応ファイル形式とアップロード制限
category: limitations
keywords:
- ファイル
- 添付
- アップロード
- 画像
- PDF
- 形式
- サイズ
- 制限
- テキスト
- コード
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: ja
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## 要点

チャットでは対応する画像、PDF、プレーンテキスト／ソースコードを添付できます。Word、オフィスの表計算、音声、動画、圧縮ファイルには対応しません。アカウントと会話でアップロードが有効な必要があります。

## 注意事項

- **対応形式**：一般的な画像、PDF、TXT、Markdown、CSV、JSON、XML、HTML、Python、JavaScript/TypeScript、CSS、SQL、YAML、設定ファイル、ログ、シェルスクリプト、一般的なソースコード拡張子。
- **画像**：1メッセージ10枚まで。自動処理前に各20 MB未満、50メガピクセル以下。可能な画像はプロバイダーの入力制限まで縮小し、できなければ拒否します。
- **PDF**：1メッセージ3個まで、各25 MB未満。処理上限は1メッセージ1,000ページです。
- **テキスト／コード**：1メッセージ3個まで、各2 MB未満。
- **合計**：各種の上限を組み合わせて最大16添付。
- **貼り付け**：クリップボードから直接貼り付けられるのは画像で、他の種類には対応しません。
- **プロバイダー**：xAI（Grok）はWebPをJPEGに自動変換します。GPT／xAIのPDFは自動でOpenRouterを経由します。
- **権限**：ファイル添付はアカウントの権限です。ボタンがなければ管理者に相談してください。
- **モード**：Multi-AIとGranSabioでは添付できません。

## 関連項目

- limitations_unsupported_features
- limitations_free_models
- file_uploads
