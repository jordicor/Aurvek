---
id: settings_billing
title: "Consulta del saldo y adición de fondos"
category: billing
keywords:
  - "saldo"
  - "facturación"
  - "pago"
  - "añadir fondos"
  - "recargar"
  - "Stripe"
  - "uso"
  - "gasto"
  - "coste"
  - "código de descuento"
  - "almacenamiento"
  - "cuota"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: es
base_source_hash: e96e5bbf094f8e7ea5f52f3d25678bcbf9063a1b9612b455d37dead0db96a14e
---

## Respuesta breve

Consulta saldo y uso en **Uso y facturación**. Para añadir fondos, pulsa **Añadir fondos** o abre `/payment`, elige entre $5 y $500 y paga con Stripe.

## Pasos

### Consultar saldo y uso

1. Abre **Configuración > Uso y facturación**; arriba aparece el **Saldo actual**.
2. Filtra por 7, 30, 90 días o todo el periodo.
3. Revisa operaciones, tokens, gasto total, coste diario medio y la tendencia diaria.
4. **Almacenamiento** muestra el espacio usado y la cuota; **Uso por tipo** separa tokens, TTS, STT, imágenes, vídeo y dominios.
5. **Actividad reciente** muestra operaciones y costes por día.

### Añadir fondos

1. Pulsa **Añadir fondos** o abre `/payment`.
2. Elige $5, $10, $25, $50 o $100, o escribe entre $5 y $500.
3. Aplica un **Código de descuento**, si tienes uno, y revisa el total.
4. Pulsa **Pagar con Stripe**. Al volver a Aurvek, el saldo se acredita al instante.

## Notas

- Stripe procesa los pagos; Aurvek no guarda los datos de la tarjeta.
- Un descuento del 100 % acredita el saldo sin redirigir a Stripe.
- El saldo se muestra en dólares con tres decimales, por ejemplo $12.450.
- En Perfil es de solo lectura; añade fondos aquí o en `/payment`.
- Los archivos y medios guardados cuentan para la cuota configurada.

## Relacionado

- settings_profile
- settings_api_keys
