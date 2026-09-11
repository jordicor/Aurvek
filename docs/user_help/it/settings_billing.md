---
id: settings_billing
title: "Controllare il saldo e aggiungere fondi"
category: billing
keywords:
  - "saldo"
  - "fatturazione"
  - "pagamento"
  - "aggiungere fondi"
  - "ricaricare"
  - "Stripe"
  - "utilizzo"
  - "spesa"
  - "costo"
  - "codice sconto"
  - "spazio"
  - "quota"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: it
base_source_hash: e96e5bbf094f8e7ea5f52f3d25678bcbf9063a1b9612b455d37dead0db96a14e
---

## Risposta breve

Saldo e utilizzo sono nella scheda **Utilizzo e fatturazione**. Per ricaricare, premi **Aggiungi fondi** o apri `/payment`, scegli fra $5 e $500 e paga con Stripe.

## Passaggi

1. Apri **Impostazioni > Utilizzo e fatturazione**: consulta **Saldo corrente** e filtra 7, 30, 90 giorni o tutto il periodo.
2. Controlla operazioni, token, spesa totale, media giornaliera, spazio/quota, ripartizione IA/TTS/STT/immagini/video/domini, andamento e attività recente.
3. Premi **Aggiungi fondi** o vai a `/payment`. Scegli $5, $10, $25, $50, $100 o un importo personalizzato da $5 a $500.
4. Inserisci eventualmente un **Codice sconto**, premi **Applica**, verifica il totale e **Paga con Stripe**. Al ritorno, il saldo viene accreditato subito.

## Note

- Stripe elabora i pagamenti; Aurvek non conserva i dati della carta.
- Uno sconto del 100% accredita senza reindirizzamento Stripe.
- Il saldo è in dollari USA con tre decimali, per esempio $12.450.
- Nel Profilo il saldo è di sola lettura. La quota dipende dall’account; caricamenti e media generati occupano spazio.

## Articoli correlati

- settings_profile
- settings_api_keys
