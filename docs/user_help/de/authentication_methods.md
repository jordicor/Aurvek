---
id: authentication_methods
title: Verfügbare Anmeldemethoden
category: auth
keywords:
- Anmeldung
- Einloggen
- Passwort
- Magic Link
- Google
- OAuth
- Authentifizierung
- Registrierung
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: de
base_source_hash: 933920d39b7aa102fa1679b27c1eec45d2aa0b9d4e69c73fa554f7fd3127ca8b
---

## Kurzantwort

Aurvek unterstützt Benutzername/Passwort, Magic Links (einmalige Anmelde-URLs) und Google-Anmeldung. Die Kontoeinstellungen des Administrators bestimmen die verfügbaren Methoden.

## Authentifizierungsmodi

Der Administrator legt einen Modus fest:
- **Nur Magic Link**: Eine individuelle URL meldet Sie direkt an, ohne Passwort.
- **Nur Passwort**: Anmeldung mit Benutzername und Passwort.
- **Magic Link + Passwort**: Beide Methoden sind nutzbar.

Ist **Google OAuth** für die Instanz aktiviert, erscheint unabhängig vom Modus **Mit Google anmelden** auf der Anmeldeseite.

## Schritte

### Mit Passwort
1. Anmeldeseite öffnen.
2. **Benutzername** und **Passwort** eingeben, **Anmelden** anklicken.

### Mit Magic Link
1. URL per E-Mail oder vom Administrator erhalten.
2. Link öffnen oder im Browser einfügen; die Anmeldung erfolgt automatisch.
3. Links gelten 3 Tage. Bei Ablauf unter **Magic Link wiederherstellen** (`/magic-link-recovery`) die E-Mail-Adresse eingeben; ein neuer Link wird zugeschickt.

### Mit Google
1. **Mit Google anmelden** anklicken.
2. Google-Konto wählen und Aurvek autorisieren.
3. Entspricht die Google-E-Mail einem Aurvek-Konto, wird es verknüpft und angemeldet. Andernfalls entsteht automatisch ein neues Konto.

### Neues Konto
1. Auf der Anmeldeseite **Registrieren** anklicken.
2. E-Mail und Pflichtfelder ausfüllen und absenden.
3. Bei aktiviertem Google OAuth geht dies auch über **Mit Google anmelden** auf der Registrierungsseite.

## Hinweise

- Sitzungen gelten bis zu 30 Tage; danach erneut anmelden.
- Nach Registrierung über Google werden Sie bei der ersten Anmeldung zur Einrichtung eines Passworts für die direkte Anmeldung aufgefordert.
- Falls erlaubt: Passwort unter **Einstellungen > Profil > Passwort ändern** ändern.
- Je nach Instanz schützt Cloudflare Turnstile oder Google reCAPTCHA die Anmeldung.
- Der Administrator bestimmt den Authentifizierungsmodus. Für eine andere Methode kontaktieren Sie ihn.

## Verwandte Artikel

- settings_profile
