# Onboarding a New Shop (White-Glove Runbook)

The end-to-end checklist for putting a new service business on BlueWrench.
Everything here is done BY US (white-glove); the shop only answers questions
and signs. Target timeline: **app live same day; SMS live in 1–2 weeks**
(carrier registration is the long pole — see step 4 and `A2P-10DLC.md`).

---

## 0. Collect from the shop (intake call)

- [ ] Shop display name (what customers see, e.g. "Acme Bike Repair")
- [ ] URL slug (lowercase, hyphens: `acme-bike-repair`)
- [ ] Owner name, email, cell
- [ ] Services + prices list (or "start from the bike-shop template")
- [ ] Sales-tax rate for their location (BB = 0.0875 NYC)
- [ ] Invoice footer text: hours, payment methods, sign-off
- [ ] Legal business info for SMS registration (see A2P-10DLC.md: EIN,
      legal name, address, website)
- [ ] Signed ToS + order form (pricing plan)

## 1. Provision the tenant (5 min)

```bash
python provision_tenant.py \
  --slug acme-bike-repair \
  --name "Acme Bike Repair" \
  --db prod \
  --tax-rate 0.0700 \
  --allowed-origin https://acme-bike-repair.bluewrenchhq.com \
  --password "<generate or let it random>"
```

This creates the admin-password secret, the tenants row, seeds the service
catalog from the bike-shop template, and registers the shop's origin in the
API Gateway CORS allow-lists.

Then set the invoice footer (until there's a UI for it):

```sql
UPDATE tenants SET invoice_footer = '<hours / payment methods / sign-off>'
WHERE slug = 'acme-bike-repair';
```

## 2. Customize the service catalog (10–30 min)

Log into the dashboard as the new shop (`?tenant=acme-bike-repair` or their
subdomain once DNS exists) → **Services** tab:

- [ ] Rename/replace template services to match their menu
- [ ] Set their prices
- [ ] Deactivate anything they don't offer (never delete — history references rows)
- [ ] Add their specials via "Add" (goes in as fixed-price)

The Service Entry form renders 100% from this catalog — verify it looks right.

## 3. Frontend URL (same day)

Until bluewrenchhq.com per-shop subdomains exist, shops use
`brooklynbikery.com/...?tenant=<slug>` (works today, ugly). When the domain
is live (see OPERATIONS.md "Per-shop domains"), add the subdomain in
CloudFront + Route53 and the same static assets serve every shop — tenant
resolution is by hostname automatically.

## 4. SMS: number + carrier registration (1–2 weeks lead time — START EARLY)

Follow `A2P-10DLC.md`. Summary:
- [ ] Buy a local Twilio number for the shop
- [ ] Register/attach their Brand + Campaign (A2P 10DLC)
- [ ] Create the auth-token secret (`bikery-twilio-<slug>`) if using a Twilio
      subaccount, else reuse the main account's secret ARN
- [ ] Update the tenants row: `twilio_account_sid`,
      `twilio_auth_token_secret_arn`, `twilio_from_number`, `sms_sender_name`
- [ ] Point the number's inbound webhook at the Admin API URL
      (`https://rqshavktfa.execute-api.us-east-1.amazonaws.com/stage/AdminDashboard`)
- [ ] Send a test invoice to YOUR phone from their tenant; confirm delivery,
      the ✓/✓✓ status ticks, and STOP → opt-out recording

**Until registration clears, the shop can use everything except SMS.** The
sendInvoice checkbox still works — invoices just aren't texted.

## 5. Handoff (30 min call)

- [ ] Walk the owner through: intake (new customer + consent checkbox),
      logging services, the invoice checkbox, Messages, the Services editor,
      and the CSV export
- [ ] Give them their login password (they can't reset it themselves yet —
      password changes go through us, see OPERATIONS.md)
- [ ] Add their owner cell to our support contact sheet
- [ ] Confirm billing is set up (manual Stripe invoice/link until billing
      automation exists)

## 6. First-week follow-up

- [ ] Day 2: check their order flow in the dashboard, ask for friction
- [ ] Day 7: review SMS delivery stats (`messages.status` counts), confirm
      no `failed` pileups
