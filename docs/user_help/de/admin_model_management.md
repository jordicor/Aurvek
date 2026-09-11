---
id: admin_model_management
title: KI-Modelle und Anbieterkataloge verwalten
category: settings
keywords:
- Modelle aktivieren
- Modelle deaktivieren
- Mehrfachauswahl
- Anbietersynchronisierung
- Modellsuche
- Katalog
- Einstellungen
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
locale: de
base_source_hash: b9815cfdd623e6fea3152e66aef9e4c6a5f32ac62477b1a95a9119179dfb9350
---

## Kurzantwort

**LLM-Verwaltung** und **Anbietersynchronisierung** aktualisieren beim Öffnen Kataloge, die älter als 24 Stunden sind. Verfügbarkeit und manuelle Anpassungen bleiben erhalten; neue Modelle sind deaktiviert. Ergebnisse erscheinen ohne Neuladen.

## Schritte

1. **Alle Modelle** öffnen, Prüfung abwarten; nach Anbieter, Name, Bilderkennung oder Verfügbarkeit filtern.
2. Zeilen oder **Alle angezeigten Modelle auswählen** markieren, dann **Ausgewählte aktivieren** oder **Ausgewählte deaktivieren**. Zeilenschalter speichern sofort.
3. Filter, Sortierung und Seitenposition bleiben erhalten. Nicht mehr passende Zeilen verschwinden; ihre Auswahl wird aufgehoben.
4. In **Anbietersynchronisierung** einen Anbieter wählen. Anzahlen und Filter **Neu**, **Aktualisiert**, **Auf dem neuesten Stand**, **Nur lokal** prüfen.
5. **Nach Updates suchen** erkennt Veröffentlichungen seit der automatischen Prüfung. **Katalog aktualisieren** ergänzt sie deaktiviert und aktualisiert Metadaten sofort. **Neu** heißt: noch nicht unter Alle Modelle vorhanden.
6. Für Verfügbarkeit Modelle an-/abwählen, dann **Änderungen speichern**. **Änderungen verwerfen** setzt offene Änderungen vor der Katalogaktualisierung zurück.

## Hinweise

- Synchronisierte Modelle lassen sich deaktivieren; nur manuelle löschen.
- **Prüfung erforderlich** kennzeichnet fehlende Metadaten. Preise vor Aktivierung prüfen.
- Aktualisierungsdaten gelten sitzungsübergreifend. Bei Fehlern bleibt der gespeicherte Katalog erhalten. Erneuter Versuch beim Besuch nach einer Stunde oder sofort per manueller Aktualisierung.
- Automatisch verwaltete Modelle, etwa verknüpfter Konten, sind hier nicht umschaltbar.
- Fehlt ein Modell in der Anbieterantwort, wird es als nur lokal markiert, ohne automatische Löschung oder Änderung der Verfügbarkeit.

## Verwandte Artikel

- chat_model_unavailable
