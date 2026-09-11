---
id: web_search_modes
title: Websuchmodi erklärt
category: search
keywords:
- native Suche
- Perplexity
- Suchmodus
- Suchmaschine
- Websuche
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: de
base_source_hash: 632298e8e9d58cedacdccbd71347370159ab69fa6929922203f4a729c8c9b12d
---

## Kurzantwort

Aurvek bietet **Nativ** und **Perplexity**. Nativ nutzt die integrierte Modellsuche und wird für die meisten Nutzer empfohlen. Perplexity verwendet einen externen Suchdienst. Wechseln Sie in den Kontoeinstellungen.

## Schritte

1. Profilicon, dann **Einstellungen** öffnen.
2. Bereich **Websuche** suchen.
3. Suchmaschine wählen:
   - **Nativ** (empfohlen): Integrierte Modellsuche, schneller und enger mit dem Gesprächskontext verbunden.
   - **Perplexity**: Leitet Anfragen an Perplexity AI (sonar-pro) als separaten Dienst. Ohne Serverkonfiguration ist die Option deaktiviert.
4. Profil speichern; Änderung gilt ab der nächsten Nachricht.

## Hinweise

- Native Suche ist für Claude, GPT und xAI verfügbar. Bei nicht unterstützten Modellen wie Gemini wird automatisch Perplexity verwendet.
- Nativ sucht das Modell selbst und integriert Ergebnisse direkt, häufig mit Quellenangaben im Text.
- Bei Perplexity ruft die KI den Dienst als Werkzeug auf und formuliert anschließend aus den Ergebnissen ihre Antwort: ein zweistufiger Ablauf.
- Die Einstellung gilt für alle Chats. Einzelne Prompts können die Suche jedoch dauerhaft ein- oder ausschalten.

## Verwandte Artikel

- web_search_usage
