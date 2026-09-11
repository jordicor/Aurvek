---
id: authentication_methods
title: "Métodos de início de sessão disponíveis"
category: auth
keywords:
  - "iniciar sessão"
  - "palavra-passe"
  - "ligação mágica"
  - "Google"
  - "OAuth"
  - "autenticação"
  - "registo"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: pt
base_source_hash: 933920d39b7aa102fa1679b27c1eec45d2aa0b9d4e69c73fa554f7fd3127ca8b
---

## Resposta breve

Aurvek permite entrar com utilizador e palavra-passe, ligação mágica de utilização única ou Google. Os métodos dependem da configuração da conta pelo administrador.

## Modos de autenticação

- **Apenas ligação mágica**: uma URL única inicia a sessão sem palavra-passe.
- **Apenas palavra-passe**: use nome de utilizador e palavra-passe.
- **Ligação mágica + palavra-passe**: ambos funcionam.

Com **Google OAuth**, aparece também **Iniciar sessão com o Google**.

## Passos

### Com palavra-passe

1. Abra a página de início de sessão, introduza **nome de utilizador** e **palavra-passe** e clique **Iniciar sessão**.

### Com ligação mágica

1. Abra a URL recebida por email ou do administrador.
2. Caduca em 3 dias. Para obter outra, abra **Recuperação de ligação mágica** (`/magic-link-recovery`) e introduza o email.

### Com Google

1. Clique **Iniciar sessão com o Google**, escolha a conta e autorize Aurvek.
2. Um email correspondente associa a conta; sem correspondência, é criada uma nova automaticamente.

### Registar uma conta

1. Clique **Registar**, preencha os campos e envie. Se disponível, também pode usar **Iniciar sessão com o Google**.

## Notas

- As sessões duram até 30 dias.
- Quem se registou com Google pode definir uma palavra-passe após a primeira entrada.
- Se permitido, altere-a em **Definições > Perfil > Alterar palavra-passe**.
- Pode haver CAPTCHA Cloudflare Turnstile ou Google reCAPTCHA.
- O administrador controla o modo; contacte-o se precisar de outro.

## Relacionado

- settings_profile
