---
id: settings_billing
title: Checking balance and adding funds
category: billing
keywords:
  - balance
  - billing
  - payment
  - add funds
  - top up
  - Stripe
  - usage
  - spending
  - cost
  - saldo
  - pago
  - recargar
  - facturacion
  - consumo
  - discount code
  - storage
  - quota
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-09-03
---

## Short answer

Your current balance and usage details are on the **Usage & Billing** tab in Settings. To add funds, click "Add Funds" or go to `/payment`, choose an amount between $5 and $500, and pay securely via Stripe.

## Steps

### Checking your balance and usage

1. Open **Settings** and click the **Usage & Billing** tab.
2. Your **Current Balance** is displayed at the top.
3. Use the **Time Period** filter (7 days, 30 days, 90 days, or all time) to adjust the reporting window.
4. Review your usage stats: total operations, tokens used, total spent, and average daily cost.
5. The **Storage** card shows space used by uploads and generated media, together with your quota when one applies.
6. The **Usage by Type** section breaks down spending across categories such as AI tokens, TTS, STT, images, video, and domains.
7. The **Spending Trend** chart shows your daily costs over the selected period.
8. **Recent Activity** lists individual days with their operation counts and costs.

### Adding funds

1. Click the **Add Funds** button on the Usage & Billing tab, or navigate to `/payment`.
2. Select a preset amount ($5, $10, $25, $50, or $100) or enter a custom amount between $5 and $500.
3. If you have a **Discount Code**, enter it and click **Apply** to see the adjusted price.
4. Review the summary showing the final amount to pay.
5. Click **Pay with Stripe** to be redirected to Stripe's secure checkout page.
6. After payment, you are returned to Aurvek and your balance is credited immediately.

## Notes

- All payments are processed through Stripe. Aurvek does not store your card details.
- If a 100% discount code is applied, the balance is credited immediately without a Stripe redirect.
- Your balance is displayed in US dollars with three decimal places (e.g., $12.450).
- The balance shown on the Profile tab is read-only. Use the Usage & Billing tab or the `/payment` page to add funds.
- Storage quota depends on your account configuration. Stored uploads and generated media count toward it.

## Related

- settings_profile
- settings_api_keys
