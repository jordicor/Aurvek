---
id: chat_export_pdf
title: 会話をPDFに書き出す
category: chat
keywords:
- PDF
- 書き出し
- ダウンロード
- エクスポート
- 会話
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: ja
base_source_hash: a6914d25a1d9ce08d3ca3dd52b52737efd1dc7a39c286cf62c88add692d16c8a
---

## 要点

どの会話もPDF文書に書き出せます。ユーザーとボットの全メッセージ、Markdownの書式、画像、コードブロック、表、日時が含まれます。

## 手順

1. サイドバーで対象会話の三点メニューを開きます。
2. **PDFをダウンロード**を選びます。
3. ダイアログで確認します。
4. バックグラウンドの生成キューに入り、開始の通知が表示されます。
5. 完了後、**メディアギャラリー**からPDFを探してダウンロードします。

## 注意事項

- 生成中もチャットを利用できます。
- ファイル名はプロンプト名と日時です（例：`My_Prompt_2026_03_22_14_30_00.pdf`）。
- メッセージ内の画像も埋め込まれます。利用できなくなった画像は代替表示になります。
- Multi-AI比較の回答も、モデルごとにラベルを付けて収録されます。
- 同じ会話の生成が進行中なら、再依頼まで数分待つ必要があります。
- 絵文字は専用フォントNoto Emojiで描画されます。

## 関連項目

- chat_export_mp3
- chat_folders
