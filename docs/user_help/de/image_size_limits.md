---
id: image_size_limits
title: Warum an die KI gesendete Bilder auf 1568 Pixel verkleinert werden
category: limitations
keywords:
- Bildgröße
- Bildqualität
- Auflösung
- verkleinertes Bild
- unscharf
- Screenshot
- '1568'
- Bildlimit
- Anbieterlimit
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
locale: de
base_source_hash: 398c00ba92296bf81e63380a1cecb2b34709488fea96970c4ba964726d3e7eba
---

## Kurzantwort

Aurvek verkleinert Chat-Bildanhänge vor dem Senden an die KI auf höchstens 1568 Pixel an der längsten Seite. Das Seitenverhältnis bleibt erhalten, nichts wird beschnitten. Das gilt nur für Bilder in Chatnachrichten; Profilbilder, Prompt-Avatare und Design-Hintergründe sind nicht betroffen.

## Hinweise

- Bei Screenshots mit kleinem Text den relevanten Bereich zuschneiden. Mehrere gezielte Ausschnitte funktionieren meist besser als ein sehr großes Bild.
- Aurvek speichert und zeigt die verkleinerte Version (höchstens 1568 px). Nach deren Speicherung bleiben die ursprünglichen hochauflösenden Bilddaten nicht auf dem Server.
- Das Limit gilt nur für hochgeladene Bilder, nicht für KI-generierte Bilder (DALL-E, Ideogram, Gemini usw.).

## Verwandte Artikel

- file_uploads
- image_generation
- limitations_file_types
