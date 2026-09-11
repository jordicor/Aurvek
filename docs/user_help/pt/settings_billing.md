---
id: settings_billing
title: "Consulta do saldo e adição de fundos"
category: billing
keywords:
  - "saldo"
  - "faturação"
  - "pagamento"
  - "adicionar fundos"
  - "carregar"
  - "Stripe"
  - "utilização"
  - "despesa"
  - "custo"
  - "desconto"
  - "armazenamento"
  - "quota"
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
locale: pt
base_source_hash: e96e5bbf094f8e7ea5f52f3d25678bcbf9063a1b9612b455d37dead0db96a14e
---

## Resposta breve

Veja saldo e utilização em **Utilização e faturação**. Para adicionar fundos, clique **Adicionar fundos** ou abra `/payment`, escolha $5 a $500 e pague com Stripe.

## Passos

### Consultar saldo e utilização

1. Abra **Definições > Utilização e faturação** para ver **Saldo atual**.
2. Filtre por 7, 30, 90 dias ou todo o período.
3. Reveja operações, tokens, total, média diária e tendência.
4. **Armazenamento** mostra espaço e quota; **Utilização por tipo** separa tokens, TTS, STT, imagens, vídeo e domínios.
5. **Atividade recente** detalha dias.

### Adicionar fundos

1. Clique **Adicionar fundos** ou abra `/payment`.
2. Escolha $5, $10, $25, $50 ou $100, ou $5 a $500 personalizados.
3. Aplique **Código de desconto**, reveja e clique **Pagar com Stripe**. O saldo é creditado ao regressar.

## Notas

- Stripe processa; Aurvek não guarda cartões.
- Desconto de 100% credita sem redirecionar.
- O saldo usa dólares e três casas, por exemplo $12.450.
- No Perfil é só leitura.
- Carregamentos e multimédia contam para a quota.

## Relacionado

- settings_profile
- settings_api_keys
