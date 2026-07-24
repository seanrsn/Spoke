# A2P 10DLC Registration (US SMS Compliance)

Every US business texting from a local (10-digit) number MUST be registered
with the carriers via Twilio's A2P 10DLC system. **Unregistered traffic gets
filtered/blocked by carriers** — invoices would silently not arrive. This is
the single longest lead-time item in onboarding: **plan 1–2 weeks**, start it
the day a shop signs.

## What gets registered

1. **Brand** — the legal business entity behind the messages.
   One brand per legal entity. Two models:
   - **Model A (per-shop brand, recommended):** each shop registers its own
     brand with its own EIN. Cleanest compliance; the shop's name shows in
     carrier records. Cost: ~$4 one-time brand fee + campaign fees.
   - **Model B (BlueWrench as ISV):** BlueWrench registers as an Independent
     Software Vendor and registers shops under its ISV profile. Better at
     scale (10+ shops); more Twilio paperwork up front.
   Start with Model A for the first few shops.

2. **Campaign** — the use case description. Ours is
   **"Customer Care / Account Notifications"**: service updates and invoices
   to existing customers who opted in at the counter. NOT marketing.
   Campaign fee: ~$2–15/month depending on type (Low-Volume Standard fits
   shops under 6,000 messages/day, which is every shop we'll onboard).

## Info to collect from the shop (before starting)

- Legal business name (exactly as registered with IRS)
- EIN (tax ID) — sole props without EIN use a different (slower) path
- Legal address
- Website URL (their own site, or their BlueWrench shop page)
- Contact name/email/phone
- Estimated monthly message volume (orders/month is a good proxy)

## Twilio console steps

1. Twilio Console → Messaging → Regulatory Compliance → A2P 10DLC
2. Register the Brand (info above). Vetting is usually instant-to-days.
3. Create the Campaign under that brand:
   - Use case: Low Volume Standard / Customer Care
   - Sample messages: paste a real invoice text and a pickup notification
   - Opt-in description: "Customers provide their phone number and consent
     in person at the counter when dropping off a repair; consent checkbox
     is recorded in our system with a timestamp."
   - Opt-out: STOP handling is automatic via Twilio + recorded by our
     webhook (customers.sms_opted_out)
4. Buy the shop's local number and attach it to the campaign's Messaging
   Service (or associate number → campaign directly).
5. Wait for campaign approval (days). Status shows in the console.

## After approval — wire into BlueWrench

Update the shop's tenants row (see ONBOARDING.md step 4) and set the number's
inbound webhook to the Admin API. Send a live test invoice; verify delivery
+ status ticks + STOP round-trip.

## Gotchas

- Campaign rejections are common on vague descriptions — use the opt-in
  language above verbatim.
- The compliance line "Reply STOP to unsubscribe" is appended to every
  invoice automatically (Backend-Form) — do not remove it.
- Toll-free numbers are an alternative path (separate verification, no
  10DLC) but look less local; stick with 10DLC local numbers.
- Brooklyn Bikery's own number is already live/grandfathered — do not touch
  its configuration when onboarding others.
