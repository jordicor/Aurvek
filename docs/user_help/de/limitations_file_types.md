---
id: limitations_file_types
title: Unterstützte Dateitypen und Upload-Limits
category: limitations
keywords:
- Datei
- Upload
- Bild
- PDF
- Dateityp
- Größenlimit
- Formate
- Anhang
- Textdatei
- Codedatei
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: de
base_source_hash: 6b207a41121e1dbaf8d1aae712481d6f0c45c0f94500a2340aed5d426f15012d
---

## Kurzantwort

Aurvek akzeptiert unterstützte Bilder, PDFs sowie Klartext- und Quellcodedateien als Chat-Anhänge. Word-Dokumente, Office-Tabellen, Audio, Video und Archive sind nicht zulässig. Uploads müssen für Konto und Chat aktiviert sein.

## Hinweise

- **Formate:** Gängige Bilder, PDFs, TXT, Markdown, CSV, JSON, XML, HTML, Python, JavaScript/TypeScript, CSS, SQL, YAML, Konfigurationsdateien, Logs, Shellskripte und übliche Quellcode-Endungen.
- **Bilder:** Bis zu 10 pro Nachricht, jeweils unter 20 MB vor automatischer Verarbeitung und höchstens 50 Megapixel. Aurvek versucht geeignete Bilder auf das Anbieterlimit zu verkleinern; andernfalls werden sie abgelehnt.
- **PDFs:** Bis zu 3 pro Nachricht, jeweils unter 25 MB; Verarbeitungslimit 1.000 Seiten pro Nachricht.
- **Text/Code:** Bis zu 3 Dateien pro Nachricht, jeweils unter 2 MB.
- **Gesamt:** Maximal 16 Anhänge bei Kombination der Höchstzahlen aller Gruppen.
- **Zwischenablage:** Bilder lassen sich direkt einfügen, andere Inhaltstypen nicht.
- **Anbieter:** xAI (Grok) wandelt WebP automatisch in JPEG um. PDFs mit GPT/xAI laufen automatisch über OpenRouter.
- **Berechtigung:** Uploads sind eine Kontoberechtigung. Fehlt die Schaltfläche, kontaktieren Sie den Administrator.
- **Modi:** Keine Anhänge in Multi-AI oder GranSabio.

## Verwandte Artikel

- limitations_unsupported_features
- limitations_free_models
- file_uploads
