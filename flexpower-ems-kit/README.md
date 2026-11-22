# Flexpower EMS Kit

A small, self-hosted energy management system for a factory or building. It runs on
one Windows PC on the plant network and:

- **reads energy meters** over **Modbus TCP** (Ethernet meters or gateways) and
  **Modbus RTU** (RS-485 through a USB adapter), several meters per bus
- **stores** every reading in a local SQLite file and keeps 15-minute kWh and
  peak-kW totals per meter indefinitely
- **analyses** consumption: kWh, cost (flat or peak/off-peak tariff), CO₂, peak
  demand, base load (what a machine draws when idle), night-time use, unmetered
  load (main meter minus sub-meters), and change on the previous period
- shows a **web dashboard** with live readings, consumption over time by machine
  or area, a ranking of the biggest consumers, a typical-day profile and CSV export
- **emails** weekly and monthly reports with plain-language findings and CSV attachments
- is published online safely through **Cloudflare Tunnel + Cloudflare Access**,
  so the owner can open it from anywhere without opening ports on the firewall

Adding a meter is one entry in `config.yaml`. The dashboard, reports and
analysis pick it up automatically.

```
 Energy meters ──RS-485 (RTU)──┐
                               ├──► Windows PC: Flexpower EMS Kit ──► SQLite
 Ethernet meters ──Modbus TCP──┘         │  dashboard on localhost:8080
                                         │  email reports via SMTP
                                         ▼
                              cloudflared (outbound only)
                                         ▼
                 https://energy.yourcompany.com  (Cloudflare Access login)
```

## Why this stack

| Choice | Reason |
|---|---|
| Python + pymodbus | Mature Modbus TCP/RTU library; same code runs on Windows and Linux |
| SQLite | No database server to install or maintain; one file to back up. Comfortably handles up to roughly 100 meters at 1-minute polling |
| Browser dashboard (FastAPI) | Nothing to install on viewers' devices. The same page works on the plant PC, a phone, and through Cloudflare |
| Cloudflare Tunnel | No port forwarding or static IP; HTTPS automatically; Access adds email/SSO login in front |
| NSSM Windows service | Starts at boot, restarts on crash, no one has to stay logged in |

## Quick start (Windows)

1. Install **Python 3.11 or newer** from python.org. Choose *Customize installation*,
   tick *Add python.exe to PATH* and, under *Advanced options*, **Install Python for all users**
   (the background service needs this).
2. Copy this `flexpower-ems-kit` folder to the PC, e.g. `C:\FlexpowerEMS`.
3. In PowerShell in that folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
   .\.venv\Scripts\python.exe -m ems seed-demo --days 90   # optional demo history
   .\run.bat
   ```
4. Open <http://localhost:8080>. The example config uses six **simulated** meters,
   so you can explore the dashboard before any wiring is done.

Linux works the same: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m ems run`.

## Connecting real meters

Edit `config.yaml`:

```yaml
meters:
  - id: compressor_1            # short, unique, never change it once data exists
    name: "Air Compressor 1"
    group: "Utilities"          # used for the "by area" roll-up
    protocol: rtu
    serial_port: COM3
    baudrate: 9600
    parity: N
    unit_id: 2                  # Modbus slave address set on the meter
    profile: eastron_sdm630

  - id: main_incomer
    name: "Main Incomer"
    main: true                  # supply meter: used for site total and "unmetered"
    protocol: tcp
    host: 192.168.1.50
    unit_id: 1
    profile: generic_holding_float
```

- A **profile** lists which registers hold which value. Examples for Eastron
  SDM630/SDM120 and a generic TCP template are included. Copy one for your meter
  model and **check every address against the meter's Modbus manual**.
- Addresses are **zero-based**. If the manual says `30073` or "register 73", use `72`.
- `energy_kwh` (the cumulative import counter) is what consumption is calculated
  from. A meter that only exposes power still works: power is integrated over time.
- Meters on the same serial port (or the same TCP gateway) share one connection
  and are read one after another. A meter that fails is logged and skipped; the
  rest of the bus carries on.
- Remove the simulated meters once real ones are in.

Test a meter before relying on it:

```powershell
.\.venv\Scripts\python.exe -m ems test-meter compressor_1
#   energy_kwh     48211.3
#   power_kw       14.82
#   ...
```

If values look 1000× off, adjust `scale`. If they look like nonsense, try
`word_order: little` or a different `type`.

## Email reports

```yaml
email:
  enabled: true
  smtp_host: smtp.office365.com     # or smtp.gmail.com, your ISP's server...
  smtp_port: 587
  security: starttls
  username: energy@yourcompany.com
  password_file: data/smtp.secret   # written encrypted by set-smtp-password
reports:
  weekly:  {enabled: true, day_of_week: mon, time: "07:00", recipients: [manager@...]}
  monthly: {enabled: true, day_of_month: 1, time: "07:30", recipients: [owner@...]}
```

Store the mail password **encrypted** (it is never written to `config.yaml`):

```powershell
.\.venv\Scripts\python.exe -m ems set-smtp-password
```

On Windows this uses DPAPI, so the file is unreadable if copied to another PC.
For Gmail/Microsoft 365 use an **app password** on a dedicated mailbox that is
only used for sending these reports. Preview without sending:

```powershell
.\.venv\Scripts\python.exe -m ems report monthly --html monthly.html
.\.venv\Scripts\python.exe -m ems report weekly --send --to you@yourcompany.com
```

The dashboard's **Report preview** button shows last month's email, and admins
can trigger a send from **Send report…**.

## Run as a Windows service

Download [NSSM](https://nssm.cc/download), put `nssm.exe` on PATH, then in an
**Administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1
```

The script:

- runs the service under its own **restricted account** (`NT SERVICE\FlexpowerEMS`)
  rather than the all-powerful LocalSystem, so a flaw in the app can't take over the PC
- locks the program folder: only Administrators, SYSTEM, you and the service can
  read it, and only the service can write to `data\`
- offers to store the email password encrypted, and makes that file readable only
  by Administrators and the service. The password never appears on a command
  line, in the registry or in PowerShell history.

Re-run it any time; it replaces the existing service. If an older version was
installed with `-SmtpPassword`, re-running removes that plain-text copy.
If an RS-485 adapter won't open under the restricted account, add `-UseLocalSystem`
as a fallback.

Because the folder is locked down, edit `config.yaml` from an editor opened as
Administrator (or as the user who ran the script), then `nssm restart FlexpowerEMS`.
Logs are in `data\ems.log`, including every sign-in, failed sign-in and manual report send.

## Publishing online with Cloudflare

You need a domain on Cloudflare (free plan is enough).

1. **Create the tunnel.** Cloudflare dashboard → *Zero Trust* → *Networks* →
   *Tunnels* → *Create a tunnel* → *Cloudflared*. Name it `flexpower-ems-kit`.
2. **Install the connector on the PC.** The page shows a Windows command like
   `cloudflared.exe service install <token>`. Run it in an Administrator prompt.
   It installs a service that connects **outbound**, so nothing has to be opened
   on the router.
3. **Public hostname.** In the tunnel, add *Public hostname*:
   `energy.yourcompany.com` → Service `HTTP` → `localhost:8080`.
4. **Protect it with Access.** *Zero Trust* → *Access* → *Applications* → *Add* →
   *Self-hosted*, domain `energy.yourcompany.com`. Add a policy *Allow* →
   *Emails* → the owner's and managers' addresses. They log in with a one-time
   code sent to their email; no passwords to manage. Set the session to e.g. 30 days.
5. Put the URL in `email.dashboard_url` so reports link to it.

Keep `web.host: 127.0.0.1` so the dashboard is only reachable through the tunnel
(and from the PC itself). To also allow PCs on the plant LAN, set `0.0.0.0` and
add dashboard users.

**Dashboard users (second layer).** Generate a hash and add it to `web.users`:

```powershell
.\.venv\Scripts\python.exe -m ems hash-password
```

`viewer` can see everything; `admin` can also send reports. With Cloudflare
Access in front you may leave the user list empty.

How sign-in is protected:

- **Sign-in page and sessions.** Users sign in on a proper page and get a session
  that ends after 12 hours of inactivity or 7 days in total (`session_idle_hours`,
  `session_max_days`). **Sign out** ends it on the server immediately. Removing a
  user from `config.yaml` and restarting signs them out everywhere.
- **Lockout.** After 5 wrong passwords in 15 minutes the username *and* the
  visitor's IP address are locked for 15 minutes (`login_max_attempts`,
  `login_lockout_minutes`). Behind Cloudflare the real visitor IP is used.
  Someone guessing can also lock out the real user for that time, which is
  another reason to keep Cloudflare Access in front.
- **Cross-site protection.** Actions such as sending a report need a per-session
  token and are refused if they come from another website, so a malicious page
  can't act for a logged-in admin.
- Session cookies are HttpOnly and, over HTTPS, Secure. Only a hash of the cookie is
  stored, so a copied database can't be used to sign in.

## Backups

Everything is in `data\ems.sqlite3` plus your `config.yaml`. Copy both to a
network share or cloud folder nightly (Task Scheduler + `robocopy` is enough).

## Project layout

```
ems/
  collector.py   Modbus TCP/RTU polling, register decoding, simulator
  db.py          SQLite schema, 15-minute aggregation
  analytics.py   periods, summaries, insights, time series, profiles
  reports.py     HTML email + CSV, SMTP sending
  scheduler.py   weekly/monthly jobs, data retention
  web.py         dashboard + JSON API + login
  static/        dashboard page (Chart.js bundled, works offline)
deploy/windows/  install and service scripts
tests/           unit tests and a real Modbus TCP round-trip test
```

Run the tests with `pip install -r requirements-dev.txt && pytest`.

## Known limits and next steps

- Consumption between two readings is credited to the 15-minute slot of the
  later reading. After a long outage the catch-up lands in one slot. Totals stay correct.
- Not yet professionally security tested. Plain-HTTP access on the LAN
  (`web.host: 0.0.0.0` without Cloudflare) sends passwords unencrypted; prefer the tunnel.
- Alerts (meter offline, consumption above a threshold, peak-demand warnings)
  are not built yet; the data needed for them is already collected.
- One site per installation. For several sites, run one instance per site, each
  with its own tunnel hostname.
- Beyond roughly 100 meters, or with much faster polling, move to
  PostgreSQL/TimescaleDB. The analytics code only touches the `intervals` table,
  so that is a contained change.
