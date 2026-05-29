# World Rankings — Cross-Tournament Player Performance

A separate, standalone app that aggregates **individual player performance across multiple big team tournaments** (WTC, WoW, and future ones), so players can track a reliable running score over the years.

> **Hard rule:** a tournament's scores only count toward ratings if we have **individual board pairings** (who played whom), so Strength of Schedule (SoS) can be computed. This is the same idea as WTC's existing `match_data_available` flag. Summary-only events can be shown for the historical record but are **excluded from ratings**.

---

## Background

- **WTC** — World Team Championship, run via the existing `scoring.hala.dk` Flask app.
- **WoW** — a 3-man team tournament run by Russel Wright, who wants a unified list of player performance across all big team competitions.
- The system must be ready to add **more tournaments** later with no schema changes.

### What already exists in scoring.hala.dk (reused conceptually)

The WTC app's `/performance` page is already a cross-edition, opponent-adjusted SoS rating engine:

```
rate r  = (W + 0.5*D) / games            per person, 0..1 (draw = half)
SoS     = mean of your opponents' own r   (~0.5 = league average)
Perf    = r + LAMBDA * (SoS - 0.5)        opponent-adjusted rate
Rating  = 1000 + 1000 * (Perf - 0.5)      Elo-like, centred on 1000
```

Key properties we keep:
- **Per-board rate** normalizes team-size differences automatically (WoW 3-man vs WTC larger teams don't need special handling — a person's rate is per individual game).
- **`match_data_available`** already enforces the SoS rule for WTC editions.
- **`people` / `person_id`** links the same human across editions.

---

## Architecture: system-of-record vs. aggregator

```
┌─────────────────────────┐          ┌──────────────────────────────┐
│  scoring.hala.dk (WTC)  │          │  World Rankings (new app)    │
│  - runs the tournament  │ ──────▶ │  - ingests many sources      │
│  - owns WTC data        │ export   │  - owns cross-tourn identity │
│  - stays as-is          │  .json   │  - owns the rating engine    │
└─────────────────────────┘          │  - WoW + future tournaments  │
                                     └──────────────────────────────┘
WoW data, future tournaments ───────────────▲
```

- **scoring.hala.dk becomes just a data source.** It never learns that WoW exists. Its DB stays pristine.
- The **new app is the only place** that knows about multiple tournaments, cross-tournament identity, and the rankings.

### Decided choices (2026-05-29)

| Decision | Choice |
|---|---|
| Where WoW/other data lives | **Not** in the scoring DB — a separate app & DB. |
| Data flow from WTC | Public read-only `GET /api/export.json` on scoring.hala.dk; rankings app pulls + upserts via an **"Import from WTC"** button. No DB coupling, no shared credentials. |
| Project structure | **New GitHub repo + new Postgres DB + new systemd service** on the same Hetzner VPS, own subdomain (e.g. `rankings.hala.dk`). |
| Stack | Lighter Flask app (read/import/rank only — no pairings, no OMR scanner). Reuses `q()` / `get_db()` patterns. |
| SoS model | **Three models behind a page toggle** (see below). |
| Identity | Owned by the rankings app; WTC `person_id` carried as `source_key`. |

**Sync cadence:** WTC and WoW each run once a year, so a manual "Import" action is plenty — no live API or DB coupling needed. scoring.hala.dk can be offline without affecting rankings.

---

## The universal interface

Every source is transformed into these tables. The rating engine reads **only** these, so adding a tournament = a new importer with **zero schema change**.

```sql
competitor(
    id,
    display_name,
    ...
);                                  -- global identity, owned by the aggregator

source_competitor(
    source,             -- 'wtc' / 'wow'
    source_key,         -- WTC: the WTC person_id   |  WoW: name (until linked)
    name,
    competitor_id       -- the cross-source link
);

game(
    id,
    source,             -- 'wtc' / 'wow'
    event_year,
    round_order,        -- for Elo chronology: (event_year, round_order)
    competitor_a,
    competitor_b,
    outcome,            -- W/D/L from competitor_a's view
    dice_kills_a,       -- optional, for tiebreaks/display
    dice_kills_b,
    rated BOOLEAN       -- = old match_data_available
);
```

- WTC board pairings → `game` rows. WoW pairings → `game` rows.
- **WTC's `person_id` is preserved as `source_key`**, so all WTC-internal dedup work carries over for free; the aggregator only does the *cross-source* bridging on top.

---

## Strength of Schedule: three models behind a toggle

Computing all three server-side is essentially free (hundreds of people, low thousands of games). Default to **iterative**. Letting viewers switch models is itself a feature — it visibly demonstrates why a weakly-connected tournament should be treated carefully.

### 1. One-deep SoS (current WTC formula)
```
sos[p]  = mean(raw_rate of opponents)
perf[p] = own_rate[p] + LAMBDA * (sos - 0.5)
```
- Pros: simplest, most transparent.
- Cons: poor cross-tournament normalization if the tournament pools barely overlap — a dominant player in an isolated/weaker pool can look inflated.

### 2. Iterative SoS (default — recommended)
```
rate_{i+1}[p] = own_rate[p] + LAMBDA * (mean(adjusted_rate of opponents) - 0.5)
# iterate to a fixed point (~5–10 passes)
```
- Recomputes SoS from opponents' **adjusted** rate, not their raw rate.
- Propagates strength across the few "connector" players who play both WTC and WoW, so an isolated weaker pool can't inflate ratings.
- Pros: fixes cross-pool connectivity; small, transparent change; deterministic.
- Cons: slightly less intuitive than one-deep.

### 3. Sequential Elo
```
for each rated game in chronological order (event_year, round_order):
    expected = 1 / (1 + 10^((Rb - Ra) / 400))
    Ra += K * (score - expected)
```
- Pros: gold-standard connectivity, time-aware.
- Cons: order-dependent, needs K tuning, more "history" than "clean per-period record." Uses `(event_year, round_order)` as the timeline — no exact dates needed.

### Qualification
Reuse the qualified/provisional split (`MIN_GAMES`, `MIN_EDITIONS`) so thin samples don't pollute the top of the table.

---

## Identity is the linchpin (and the main risk)

Cross-tournament ranking is only as reliable as "person X at WTC 2024 == person X at WoW 2025."

- WTC export gives WTC names + WTC `person_id` (already deduped within WTC across years).
- WoW gives WoW names.
- The aggregator links them via `source_competitor → competitor`, with an **alias map** (same pattern as the WTC 2022 "Peter Benett → Peter Barrett" fix) and an **admin review screen for unmatched players** before a new tournament's edition counts.
- No silent auto-merge — mistakes here become wrong ratings.

This merge/match UI must be **rebuilt in the new app** (the WTC screens can't be reused directly).

---

## The `/api/export.json` contract (scoring.hala.dk side)

The **only** change that lands in the scoring repo. Read-only, public (standings already are), no auth. Sits next to the existing public routes. Pairings nested under their edition (simplest for the importer):

```jsonc
{
  "source": "wtc",
  "editions": [
    {
      "id": 7,
      "year": 2026,
      "name": "WTC 2026",
      "match_data_available": true,
      "players": [
        { "id": 1421, "name": "Lars Andreasen", "country": "DK", "person_id": 312 }
      ],
      "pairings": [
        {
          "round": 1,
          "board_number": 3,
          "player_a": 1421,
          "player_b": 1502,
          "outcome": "W",          // from player_a's view (W/D/L)
          "dice_kills_a": 4,
          "dice_kills_b": 2
        }
      ]
    }
  ]
}
```

> Final field names confirmed against `players`, `player_matches`, `editions`, `rounds` at build time.

---

## Phases

| Phase | Where | Status | Deliverable |
|---|---|---|---|
| **0** | scoring repo | ✅ Done | Read-only `GET /api/export.json` — editions + players w/ `person_id` + board pairings + `match_data_available`. |
| **1** | rankings repo | ✅ Done | Flask skeleton + Postgres schema (`competitor`, `source_competitor`, `source_edition`, `game`). |
| **2** | rankings repo | ✅ Done | WTC importer (`importer_wtc.py`) — pulls `export.json` → upserts `game` rows, carries WTC `person_id` as `source_key`. |
| **3** | rankings repo | ✅ Done | Rating engine (3 models) + `/world` page with model toggle + tournament filter. |
| **4** | rankings repo | ⏳ Blocked on WoW data | WoW importer (`importer_wow.py`) + cross-source identity review. `/admin/unmatched` screen already built. |
| **5** | VPS | ⏳ To do | Deploy to `rankings.hala.dk` (see below). |

Repo: **https://github.com/Gubisoul07/Bolt-Action-World-Rankings**

---

## Deploy to rankings.hala.dk (Phase 5)

Everything needed to get the app running on the same Hetzner VPS as `scoring.hala.dk`.

### 1. SSH onto the VPS and clone the repo

```bash
ssh root@178.104.159.45
git clone https://github.com/Gubisoul07/Bolt-Action-World-Rankings.git /opt/rankings
cd /opt/rankings
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Create the Postgres database

```bash
sudo -u postgres psql
```
```sql
CREATE DATABASE rankings;
CREATE USER rankings_user WITH PASSWORD 'choose-a-strong-password';
GRANT ALL PRIVILEGES ON DATABASE rankings TO rankings_user;
\q
```

### 3. Create the `.env` file

```bash
cat > /opt/rankings/.env << EOF
SECRET_KEY=choose-a-long-random-string
ADMIN_PASSWORD=choose-an-admin-password
DATABASE_URL=postgresql://rankings_user:choose-a-strong-password@localhost/rankings
EOF
chmod 600 /opt/rankings/.env
```

### 4. Create the systemd service

```bash
cat > /etc/systemd/system/rankings.service << EOF
[Unit]
Description=Bolt Action World Rankings
After=network.target postgresql.service

[Service]
User=www-data
WorkingDirectory=/opt/rankings
EnvironmentFile=/opt/rankings/.env
ExecStart=/opt/rankings/venv/bin/gunicorn -w 2 -b 127.0.0.1:5001 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rankings
systemctl start rankings
systemctl status rankings
```

### 5. Add the nginx vhost

```bash
cat > /etc/nginx/sites-available/rankings.hala.dk << EOF
server {
    listen 80;
    server_name rankings.hala.dk;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -s /etc/nginx/sites-available/rankings.hala.dk /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 6. Point DNS at the VPS

Add an **A record** for `rankings.hala.dk` → `178.104.159.45` in your DNS provider.

### 7. Issue a TLS certificate

```bash
certbot --nginx -d rankings.hala.dk
```

### 8. Verify

- https://rankings.hala.dk — home page shows "No events imported yet."
- Log in with your `ADMIN_PASSWORD` and click **Import from WTC** to pull live data from scoring.hala.dk.

### Deploy updates (after initial setup)

```bash
ssh root@178.104.159.45 "cd /opt/rankings && git pull origin master && systemctl restart rankings"
```

---

## Phase 4 — WoW importer (blocked on data)

Once Russel provides data, create `importer_wow.py` following the same pattern as `importer_wtc.py`:
- If he can provide **per-round board pairings** → full rated import (`match_data_available=1`).
- If he can only provide **final standings** → history-only import (`match_data_available=0`), excluded from ratings per the SoS rule.

After import, use `/admin/unmatched` to link WoW player names to existing WTC competitors (or create new global competitors for WoW-only players).

---

## Open items

- **WoW data format** — does Russel have per-round pairings or only final standings?
- **Subdomain confirmed?** Plan assumes `rankings.hala.dk`.

---

## Net cost vs. keeping it in the scoring DB

- **+1 export endpoint** on the WTC side (done).
- **Separate deploy** on the same VPS (Phase 5 above).
- **A little duplicated rating math** (~40 lines, pure — copied, not shared as a package).

In return: the scoring DB stays pristine and focused on running tournaments, and the rankings product can evolve (and be owned, e.g. by Russel) independently.
