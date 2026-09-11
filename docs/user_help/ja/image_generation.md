---
id: image_generation
title: AIで画像を生成する
category: media
keywords:
- 画像
- 生成
- 作成
- DALL-E
- Gemini
- Ideogram
- 縦横比
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: ja
base_source_hash: 7adc994c28f9795f01260f7c84f2a0d0b628308e592ed19a5c780e0a380453ac
---

## 要点

会話で欲しい画像を説明すると、AIが意図を読み取り、画像を作ってチャット内に表示します。必要なら縦横比も指定できます。

## 手順

1. 会話を開きます。
2. 「海に沈む夕日の画像を生成して」「帽子をかぶった猫の肖像を作って」など、欲しい画像を説明します。
3. 縦横比は`width:height`形式で指定します。対応比率は`1:1`、`16:9`、`9:16`、`4:3`、`3:4`です。例：「山の風景を16:9で描いて」。
4. 送信すると、作成中は「画像を生成中…」と表示されます。
5. 完成した画像がチャットに表示されます。

## 注意事項

- 十分な残高が必要で、不足するとエラーになります。
- AIは内容から生成を判断します。専用コマンドは不要で、自然な言葉で依頼できます。
- 比率を省略すると1:1（正方形）です。
- エンジンにより数秒かかります。長すぎる場合はタイムアウトが表示されます。
- 画像は会話に保存され、後から見られます。

## 関連項目

- video_generation
- plus_menu_overview
