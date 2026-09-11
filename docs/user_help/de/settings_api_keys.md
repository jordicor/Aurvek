---
id: settings_api_keys
title: Eigene API-Schlüssel verwenden (BYOK)
category: settings
keywords:
- API-Schlüssel
- BYOK
- eigene Schlüssel
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
- Zugangsdaten
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: de
base_source_hash: 8d6ae4ed5c1d21ca604207d455d6d0683c5f58383e241db56f859b1f39e29dd1
---

## Kurzantwort

Falls Ihr Konto es erlaubt, richten Sie eigene Anbieterschlüssel unter **Einstellungen > API-Schlüssel** oder `/api-credentials` ein. Schlüssel eingeben, Speichermodus wählen und speichern. Sie werden statt der Standardschlüssel der Plattform verwendet.

## Schritte

1. **Einstellungen > API-Schlüssel** öffnen.
2. **Speichermodus** wählen:
   - **Nur Sitzung:** Schlüssel verschwinden beim Schließen des Browsertabs.
   - **Dauerhaft im Browser:** Bleiben sitzungsübergreifend bis zum Löschen.
   - **Serverspeicherung:** Verschlüsselt auf dem Server, von jedem Gerät erreichbar.
3. Schlüssel für einen oder mehrere Anbieter eingeben:
   - **OpenAI:** unterstützte GPT-, Bild- und Sprachdienste.
   - **Anthropic:** Claude.
   - **Google AI:** Gemini.
   - **xAI:** Grok.
   - **MiniMax**, **Kimi:** jeweils unterstützte Modelle.
   - **ElevenLabs:** Sprachausgabe und Stimmklonen.
4. **Testen** neben dem Schlüssel prüft ihn vor dem Speichern.
5. **Alle speichern** speichert Eingaben; **Alle testen** prüft alle Schlüssel.
6. Einzelnen Schlüssel mit **X** beim Anbieter entfernen, dann speichern.
7. **Alle entfernen** entfernt alle Schlüssel.

## Hinweise

- Der Administrator wählt **Nur Systemschlüssel** (kein BYOK), **Nur eigene Schlüssel** (BYOK Pflicht), **Beide, eigene bevorzugen** oder **Beide, System bevorzugen**.
- Bei „Nur Systemschlüssel“ erscheint eine Information; Einrichtung entfällt.
- Sind eigene Schlüssel Pflicht und fehlen, erscheint ein Warnbanner.
- **API-Schlüssel erhalten** verlinkt je Anbieter dessen Schlüsselverwaltung.

## Verwandte Artikel

- settings_profile
- settings_billing
