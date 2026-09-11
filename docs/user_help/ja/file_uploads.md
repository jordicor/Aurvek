---
id: file_uploads
title: 会話にファイルを添付する
category: chat
keywords:
- 添付
- ファイル
- アップロード
- 画像
- PDF
- 貼り付け
- テキスト
- コード
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
prerequisites:
- ファイル添付が有効で、選択したモデルまたはモードが添付形式に対応していること
locale: ja
base_source_hash: 8f51307b57750f50741c6086814ebd8518a268777d3ede84ac3f3b0928d7bde5
---

## 要点

対応する画像、PDF、テキスト／コードファイルを添付できます。**+ > ファイルを添付**を使うか、クリップボードの画像を貼り付けます。送信前にプレビューが表示されます。

## 手順

1. 添付を利用できる会話を開きます。
2. **+ > ファイルを添付**でファイル選択を開くか、画像を直接貼り付けます（Ctrl+VまたはCmd+V）。
3. 対応する画像、PDF、テキスト／コードを1つ以上選びます。
4. 入力欄の上に、画像ならサムネイル、それ以外はファイル名が表示されます。
5. 必要に応じてメッセージを入力して送信します。AIは添付とテキストを一緒に処理します。

## 注意事項

- 現在のサイズ・数量制限は`limitations_file_types`にあります。
- 選択モデルが添付に対応する必要があります。Multi-AIとGranSabioの会話はテキストのみです。
- クリップボードから貼り付けられるのは画像で、PDFではありません。

## 関連項目

- plus_menu_overview
- limitations_file_types
