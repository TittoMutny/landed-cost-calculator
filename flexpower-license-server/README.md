# Flexpower licence server

Runs at Flexpower, **not** at customers. It decides which Flexpower EMS Kit
installations may run, and lets staff manage subscriptions:

- add customers and generate licence keys
- **M-Pesa payments that unlock automatically**: "Pay now" prompts from the customer's
  lock screen (STK push) and Paybill payments (C2B), turned into months at the plan price
- record cash or bank payments by hand, which extends "paid until"
- **pause / resume** a subscription with one click, with a reason the customer sees
- automatic pause when a customer is unpaid past their grace period
- plans with meter limits and features (email reports, SCADA/MQTT connections)
- see each site's PC, app version, meters in use and last check-in
- full history of every action

```
EMS PC (customer) ──HTTPS every 6 h──► licence server ──► signed answer: active / overdue / paused / cancelled
                                            ▲
Flexpower staff ── /admin (browser) ────────┘
```

## How a pause reaches the customer

1. Staff click **Pause now**, or a customer passes `paid until` + grace days.
2. At the site's next check-in (within `check_hours`, default 6 h), or immediately
   when someone at the site clicks **Check again**, the EMS receives a signed
   "paused" answer and locks the dashboard, email reports and SCADA/MQTT feeds.
   Meters keep recording.
3. Staff click **Record payment** (or **Resume**), and the site unlocks at its next check-in.

A site that can't reach the server keeps its last answer for up to `offline_days`
(default 7), but never past its pause date. Unplugging the internet doesn't avoid a pause.

## Setup

Needs Python 3.11+. Host it on a small Linux/Windows VPS, or on an office PC
published with **Cloudflare Tunnel** (same steps as for the EMS dashboard), e.g. at
`https://license.flexpower.co.ke`.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m license_server init --ems ../flexpower-ems-kit     # signing key + config; embeds the public key in the EMS
.venv/bin/python -m license_server hash-password                       # paste into license-server.yaml under admins:
.venv/bin/python -m license_server run --host 127.0.0.1 --port 8443
```

Then open `https://<your licence host>/admin`.

- `init` creates `data/signing_key.pem`. **Back it up somewhere safe and never share it.**
  Anyone with it can create valid licences. If it's lost, every customer needs an app
  update with the new public key. To encrypt the file at rest, set
  `FLEXPOWER_LICENSE_KEY_PASSPHRASE` before `init` and whenever the server runs.
- `--ems ../flexpower-ems-kit` writes the matching public key into
  `flexpower-ems-kit/ems/licensing.py`. Do this once, before building the app you ship to customers.
- Set `server_url` in each customer's `config.yaml` (`license:` section), or change
  `DEFAULT_SERVER` in `ems/licensing.py` before shipping.
- Put the support phone and email in `license-server.yaml`. They're shown on the
  customer's "paused" screen.

## M-Pesa payments (automatic unlock)

Customers can pay in two ways. Either one extends the subscription and lifts a
pause automatically, with no staff action needed:

| Way | What the customer does | How it reaches us |
|---|---|---|
| **Pay now (STK push)** | On the EMS lock screen or "payment overdue" banner, picks 1/3/6/12 months, enters their Safaricom number, then enters their M-Pesa PIN on the phone | Daraja STK push → callback → the site unlocks within seconds |
| **Paybill** | M-Pesa menu → Lipa na M-Pesa → Paybill **your number**, account **FLX0007** (shown on their lock screen and in the admin panel) | Daraja C2B confirmation → credited within a minute; the site unlocks at its next check-in or on "Check again" |

Staff can also send a payment prompt to a customer's phone (*Request M-Pesa payment*
on the customer), and still record cash or bank payments by hand.

**How money becomes months.** Each plan has a `price_month`, which you can override per
customer. A payment is added to the customer's credit, whole months are taken from it,
and any remainder stays as credit for next time. For example, KES 7,500 on a
KES 5,000 plan gives 1 month plus KES 2,500 credit.

**Safety**

- Each M-Pesa receipt is credited once only. Repeated callbacks are ignored.
- Callbacks are only accepted at URLs containing `MPESA_CALLBACK_SECRET`, and by default
  only from Safaricom's published IP addresses.
- Every STK callback is double-checked with Safaricom (STK query) before it's credited.
- If a callback is lost, the server asks Safaricom about pending payments every minute.
- Paybill payments with an unknown account number aren't lost. They appear under
  **M-Pesa payments → Unmatched** for staff to assign to the right customer. If Safaricom
  enables validation for your shortcode, unknown account numbers are rejected at payment time.
- An installation can only request payments for its own licence (key + PC must match),
  limited to one open request at a time and 5 per hour.

**Setup**

1. Create an app on the Safaricom developer portal (https://developer.safaricom.co.ke)
   and test with the sandbox shortcode first. For production you need your own Paybill
   or Till "Go Live" approval, which gives you the consumer key, secret and passkey.
2. Set these environment variables on the server (not in the YAML file):
   ```
   MPESA_CONSUMER_KEY=...  MPESA_CONSUMER_SECRET=...  MPESA_PASSKEY=...
   MPESA_CALLBACK_SECRET=<python -c "import secrets;print(secrets.token_urlsafe(32))">
   ```
3. Fill in the `mpesa:` block of `license-server.yaml`: `enabled: true`, `environment`,
   `shortcode`, `c2b_shortcode`, and `public_base_url` (the HTTPS address Safaricom can
   reach, e.g. your Cloudflare Tunnel hostname). Set prices under `plans:`.
4. Restart, open **M-Pesa payments** in the admin panel, check it says *Connected*, and
   click **Register Paybill URLs with Safaricom** once.
5. Test end to end with the sandbox before switching `environment: production`.

The Safaricom IP list and API details come from the Daraja documentation. Check them
on the Daraja portal when you go live.

## Security

- Admin panel: password login with lockout after 5 failures, HttpOnly SameSite=Strict
  session cookie, CSRF token on every change, 8-hour idle timeout. Keep it behind HTTPS
  (Cloudflare Tunnel does this). You can also put Cloudflare Access in front of `/admin`
  for staff-only access.
- Licence keys are stored only as SHA-256 fingerprints. The full key is shown once
  at creation, so copy it then.
- Unknown keys are rate-limited per IP (20 per hour).
- The check-in API is public by design. It reveals nothing without a valid key, and
  a key only works on the PC it was activated on.

## Backups

Back up `data/licenses.sqlite3` (customers, payments, history) and
`data/signing_key.pem` (offline, encrypted).

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```
