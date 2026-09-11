---
id: authentication_methods
title: "Métodos de inicio de sesión disponibles"
category: auth
keywords:
  - "iniciar sesión"
  - "contraseña"
  - "enlace mágico"
  - "Google"
  - "OAuth"
  - "autenticación"
  - "registro"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
locale: es
base_source_hash: 933920d39b7aa102fa1679b27c1eec45d2aa0b9d4e69c73fa554f7fd3127ca8b
---

## Respuesta breve

Aurvek permite iniciar sesión con usuario y contraseña, un enlace mágico de un solo uso o Google. Los métodos disponibles dependen de cómo haya configurado tu cuenta el administrador.

## Modos de autenticación

- **Solo enlace mágico**: recibes una URL única que inicia la sesión directamente, sin contraseña.
- **Solo contraseña**: usas tu nombre de usuario y contraseña.
- **Enlace mágico + contraseña**: puedes usar cualquiera de los dos.

Si la instancia tiene **Google OAuth**, también aparece **Iniciar sesión con Google**, sea cual sea el modo de la cuenta.

## Pasos

### Con contraseña

1. Abre la página de inicio de sesión.
2. Introduce tu **nombre de usuario** y **contraseña**.
3. Pulsa **Iniciar sesión**.

### Con un enlace mágico

1. Recibe la URL por correo o del administrador.
2. Ábrela o pégala en el navegador; la sesión se inicia automáticamente.
3. Caduca a los 3 días. Para obtener otra, abre **Recuperación de enlace mágico** (`/magic-link-recovery`) e introduce tu correo.

### Con Google

1. Pulsa **Iniciar sesión con Google**.
2. Elige tu cuenta y autoriza Aurvek.
3. Si el correo coincide con una cuenta de Aurvek, se vincula e inicia la sesión.
4. Si no coincide, se crea una cuenta nueva automáticamente.

### Registro de una cuenta nueva

1. Pulsa **Registrarse** en la página de acceso.
2. Completa el correo y los demás campos obligatorios y envía el formulario.
3. Si Google OAuth está disponible, también puedes registrarte con **Iniciar sesión con Google**.

## Notas

- Las sesiones duran hasta 30 días.
- Si te registraste con Google y quieres acceso directo con contraseña, se te pedirá crearla tras el primer inicio.
- Si está permitido, cambia la contraseña en **Configuración > Perfil > Cambiar contraseña**.
- La página puede usar CAPTCHA (Cloudflare Turnstile o Google reCAPTCHA), según la instancia.
- El administrador controla el modo de autenticación; contacta con él si necesitas otro.

## Relacionado

- settings_profile
