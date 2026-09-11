---
id: authentication_methods
title: "Metodi di accesso disponibili"
category: auth
keywords:
  - "accesso"
  - "password"
  - "link magico"
  - "Google"
  - "OAuth"
  - "autenticazione"
  - "registrazione"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: it
base_source_hash: 933920d39b7aa102fa1679b27c1eec45d2aa0b9d4e69c73fa554f7fd3127ca8b
---

## Risposta breve

Aurvek offre nome utente e password, link magici monouso e Google Sign-In. I metodi disponibili dipendono dalla configurazione dell’amministratore.

## Passaggi

1. L’account usa **Solo link magico**, **Solo password** o **Link magico + password**. Se Google OAuth è attivo, **Accedi con Google** compare in ogni modalità.
2. Con password, inserisci nome utente e password nella pagina di accesso e premi **Accedi**.
3. Con link magico, apri l’URL ricevuto. Scade dopo 3 giorni; usa `/magic-link-recovery` con la tua e-mail per riceverne uno nuovo.
4. Con Google, premi **Accedi con Google**, scegli l’account e autorizza Aurvek. Un indirizzo corrispondente collega l’account esistente; altrimenti ne crea uno automaticamente.
5. Per registrarti, premi **Registrati**, compila i campi e invia. Se disponibile, puoi usare Google anche dalla registrazione.

## Note

- Le sessioni durano al massimo 30 giorni.
- Dopo una registrazione Google, Aurvek può chiedere una password al primo accesso.
- Se consentito, cambiala in **Impostazioni > Profilo > Cambia password**.
- Cloudflare Turnstile o Google reCAPTCHA può proteggere l’accesso secondo la configurazione.
- L’amministratore controlla la modalità; contattalo per cambiarla.

## Articoli correlati

- settings_profile
