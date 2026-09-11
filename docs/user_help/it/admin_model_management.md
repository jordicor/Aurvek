---
id: admin_model_management
title: "Gestire i modelli IA e i cataloghi dei provider"
category: settings
keywords:
  - "abilitare modelli"
  - "disabilitare modelli"
  - "selezione multipla"
  - "sincronizzazione provider"
  - "scoperta modelli"
required_role: admin
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-07
locale: it
base_source_hash: b9815cfdd623e6fea3152e66aef9e4c6a5f32ac62477b1a95a9119179dfb9350
---

## Risposta breve

Aprendo **Gestione LLM** o **Sincronizzazione provider**, Aurvek aggiorna automaticamente i cataloghi più vecchi di 24 ore. Disponibilità e modifiche manuali esistenti restano invariate; i nuovi modelli vengono aggiunti disabilitati. Il risultato appare senza ricaricare la pagina.

## Passaggi

1. Apri **Tutti i modelli**, attendi il controllo e filtra per provider, nome, visione o disponibilità.
2. Seleziona righe singole o **Seleziona tutti i modelli mostrati**, quindi **Abilita selezionati** o **Disabilita selezionati**. Anche l’interruttore della riga salva subito.
3. Filtri, ordinamento e pagina restano invariati; le righe che non corrispondono più spariscono e vengono deselezionate.
4. In **Sincronizzazione provider**, scegli un provider e controlla **Nuovi**, **Aggiornati**, **Aggiornati all’ultima versione** e **Solo locali**.
5. **Controlla aggiornamenti** rileva le nuove versioni. **Aggiorna catalogo** le aggiunge disabilitate e aggiorna subito i metadati. **Nuovo** significa non ancora presente in Tutti i modelli.
6. Seleziona o deseleziona i modelli e premi **Salva modifiche**; **Annulla modifiche** elimina le modifiche in sospeso.

## Note

- I modelli sincronizzati si possono disabilitare; solo quelli manuali si possono eliminare.
- **Da rivedere** segnala metadati incompleti: verifica i prezzi prima di abilitare.
- Le date sono condivise tra sessioni. Dopo un errore resta il catalogo salvato; nuovo tentativo dopo un’ora o subito con aggiornamento manuale.
- I modelli gestiti automaticamente, inclusi quelli di account collegati, non sono modificabili qui.
- Se un modello manca dalla risposta del provider, diventa solo locale senza essere eliminato né cambiare disponibilità.

## Articoli correlati

- chat_model_unavailable
