---
id: settings_billing
title: Guthaben prüfen und aufladen
category: billing
keywords:
- Guthaben
- Abrechnung
- Zahlung
- Aufladen
- Stripe
- Nutzung
- Ausgaben
- Kosten
- Rabattcode
- Speicher
- Kontingent
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: de
base_source_hash: e96e5bbf094f8e7ea5f52f3d25678bcbf9063a1b9612b455d37dead0db96a14e
---

## Kurzantwort

Unter **Einstellungen > Nutzung** sehen Sie Guthaben und Nutzung. **Guthaben aufladen** oder `/payment` ermöglicht $5–$500 per sicherer Stripe-Zahlung.

## Schritte

### Guthaben und Nutzung prüfen
1. **Einstellungen > Nutzung** öffnen.
2. Oben **Aktuelles Guthaben** prüfen.
3. **Zeitraum** wählen: 7, 30, 90 Tage oder gesamter Zeitraum.
4. Vorgänge, Tokens, Gesamtausgaben und tägliche Durchschnittskosten prüfen.
5. **Speicher** zeigt Uploads und erzeugte Medien samt Kontingent, falls vorhanden.
6. **Nutzung nach Typ** gliedert Kosten nach KI-Tokens, TTS, STT, Bildern, Videos, Domains usw.
7. **Ausgabentrend** zeigt Tageskosten im gewählten Zeitraum.
8. **Letzte Aktivität** listet Tage mit Vorgangsanzahl und Kosten.

### Aufladen
1. **Guthaben aufladen** oder `/payment` öffnen.
2. $5, $10, $25, $50, $100 oder eigenen Betrag zwischen $5 und $500 wählen.
3. Falls vorhanden **Rabattcode** eingeben und **Anwenden** anklicken; angepassten Preis prüfen.
4. Endbetrag in der Zusammenfassung prüfen.
5. **Mit Stripe bezahlen** öffnet den sicheren Checkout.
6. Nach Zahlung kehren Sie zu Aurvek zurück; die Gutschrift erfolgt sofort.

## Hinweise

- Stripe verarbeitet Zahlungen; Aurvek speichert keine Kartendaten.
- Bei 100% Rabatt erfolgt die Gutschrift sofort ohne Weiterleitung zu Stripe.
- Anzeige in US-Dollar mit drei Nachkommastellen, z. B. $12.450.
- Das Guthaben im Profil ist schreibgeschützt. Aufladen unter Nutzung oder `/payment`.
- Das Speicherkontingent hängt vom Konto ab; gespeicherte Uploads und erzeugte Medien zählen dazu.

## Verwandte Artikel

- settings_profile
- settings_api_keys
