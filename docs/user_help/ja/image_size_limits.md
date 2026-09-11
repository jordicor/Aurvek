---
id: image_size_limits
title: AIに送る画像を1568ピクセルに縮小する理由
category: limitations
keywords:
- 画像
- サイズ
- 画質
- 解像度
- 縮小
- ぼやける
- スクリーンショット
- '1568'
- 制限
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
locale: ja
base_source_hash: 398c00ba92296bf81e63380a1cecb2b34709488fea96970c4ba964726d3e7eba
---

## 要点

Aurvekでチャットに添付した画像は、AIへ送る前に長辺が最大1568ピクセルになるよう縮小されます。縦横比は維持され、切り抜きません。この制限はチャットメッセージの画像のみで、プロフィール写真、プロンプトのアバター、テーマの壁紙には影響しません。

## 注意事項

- 小さな文字のスクリーンショットは、見せたい部分を切り抜いてください。巨大な1枚より、対象を絞った複数の画像の方が適しています。
- 保存・表示されるのは縮小版（1568 px以下）です。縮小版の保存後、元の高解像度データはサーバーに保持されません。
- 制限はアップロード画像のみで、AI生成画像（DALL-E、Ideogram、Geminiなど）には適用されません。

## 関連項目

- file_uploads
- image_generation
- limitations_file_types
