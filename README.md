# laptop-price-watcher

Discord-alerted price watcher for gaming/pro laptops on Quebec/Canada retailers. Scrapes Best Buy CA, Newegg CA, Canada Computers, Memory Express, and Apple CA every 2 hours via GitHub Actions; fires a rich Discord embed when a watched CPU appears at or below its tier ceiling, or drops 5%+ below the last alerted price.

## Watched CPUs

Roughly 38 chips across four tiers — Intel Core Ultra HX, AMD Ryzen HX, AMD Ryzen AI Max (Strix Halo), AMD Ryzen AI HX, Apple M4/M5, Qualcomm Snapdragon X. See `config.yaml`.

## Setup

### 1. Discord webhook

1. Open Discord, go to the server you want alerts in.
2. **Server Settings → Integrations → Webhooks → New Webhook**.
3. Name it (e.g. "laptop deals"), pick the channel, click **Copy Webhook URL**.
4. Paste into `.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
5. Treat that URL like a password — it's already in `.gitignore`.

### 2. Install

```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your webhook URL
```

### 3. Verify locally

```bash
pytest tests/                                                     # unit tests
python -m notifier.discord --test                                 # smoke-test webhook
python watcher.py --dry-run --retailer newegg_ca --debug          # dry-run one retailer
python watcher.py --dry-run --debug                               # dry-run all retailers
python watcher.py --once                                          # one real run
```

### 4. GitHub Actions

1. Push to a GitHub repo.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: same URL from step 1
3. **Actions tab → Laptop watcher → Run workflow** to trigger a canary run.
4. The schedule (`0 */2 * * *`) takes over after the canary succeeds.

## Project layout

```
laptop-price-watcher/
├── config.yaml              CPUs, ceilings, retailers
├── retailers/
│   ├── base.py              Listing dataclass, Retailer ABC
│   ├── _normalize.py        CPU canonicalizer
│   ├── _http.py             shared httpx client
│   └── *.py                 retailer adapters
├── notifier/discord.py      webhook poster + embed builder
├── store/sqlite.py          state.db, NEW/DROP diff
├── watcher.py               orchestrator
├── tests/                   pytest suite
└── .github/workflows/cron.yml
```

## Flags

```
python watcher.py --once                  # single run, then exit
python watcher.py --dry-run               # print would-be alerts, no Discord, no DB writes
python watcher.py --retailer NAME         # restrict to one retailer
python watcher.py --debug                 # print scan stats per retailer
```

## How alerts work

- **NEW**: previously-unseen `(retailer, sku)` AND price ≤ tier ceiling for that CPU.
- **DROP**: known SKU, current price is `alert_on_drop_pct`% below the last *alerted* price (not last seen — prevents re-firing on small wiggles).
- **None**: silent. Most runs.

State lives in `state.db` (SQLite), committed back to the repo at the end of each cron run.

## Adding a retailer

1. New file under `retailers/`, e.g. `costco_ca.py`.
2. Subclass `Retailer` from `retailers/base.py`, implement `search(cpu_filter) -> list[Listing]`.
3. Add `costco_ca` to `retailers:` in `config.yaml`.
4. Done — the watcher will pick it up by name.
