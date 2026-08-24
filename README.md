# laptop-price-watcher

Discord-alerted price watcher for gaming/pro laptops and gaming prebuilts in Quebec/Canada. It scans Canadian retailers twice daily, sends NEW/DROP laptop alerts, and prints component-based top-10 value lists for laptops and prebuilts. Walmart CA and Best Buy CA provide the prebuilt catalog; the laptop catalog also includes Newegg, Canada Computers, Memory Express, Apple, Lenovo, Visions, and RedFlagDeals.

## Watched CPUs

The four tiers cover Intel Core Ultra/Core HX, AMD Ryzen HX and Ryzen AI, and Apple M3/M4/M5 Pro/Max chips. Snapdragon processors are intentionally excluded. See `config.yaml` for the canonical allowlist and price ceilings.

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
cp .env.example .env   # then edit .env
```

### 3. Optional slash-command bot

The automatic daily summaries use the webhook. To enable `/laptops` and `/prebuilts`:

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications), add a bot, and copy its token.
2. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`; grant **Send Messages** and **Embed Links**, then invite it to the server.
3. Set these in `.env`:
   ```
   DISCORD_BOT_TOKEN=...
   DISCORD_GUILD_ID=...   # optional; makes command updates instant in this server
   ```
4. Run `python watcher.py --once` at least once to populate the catalog, then run `python discord_bot.py` on an always-on host.

A scheduled GitHub Actions job cannot listen for commands after it exits, so the persistent bot process is required for slash commands. Both commands read the latest catalog from `state.db`; run the watcher on the same host, mount a shared DB, or regularly pull the Action-committed `state.db` so the bot sees updates.

### 4. Verify locally

```bash
pytest tests/                                                     # unit tests
python -m notifier.discord --test                                 # smoke-test webhook
python watcher.py --dry-run --retailer newegg_ca --debug          # dry-run one retailer
python watcher.py --dry-run --debug                               # dry-run all retailers
python watcher.py --dry-run --retailer walmart_prebuilts --debug  # prebuilt smoke test
python watcher.py --once                                          # one real run + rankings
python discord_bot.py                                             # persistent slash-command bot
```

### 5. GitHub Actions

1. Push to a GitHub repo.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: same URL from step 1
3. **Actions tab → Laptop watcher → Run workflow** to trigger a canary run.
4. The twice-daily schedule (`0 13,1 * * *`) takes over after the canary succeeds.

## Project layout

```
laptop-price-watcher/
├── config.yaml              CPUs, ceilings, retailers
├── retailers/
│   ├── base.py              Listing dataclass, Retailer ABC
│   ├── _normalize.py        CPU canonicalizer
│   ├── _http.py             shared httpx client
│   ├── _browser.py          Playwright browser helper
│   └── *.py                 retailer adapters (including Walmart embedded JSON)
├── notifier/discord.py      alert + ranked-list embed builders
├── ranking.py               transparent component-value formula/tables
├── discord_bot.py           /laptops and /prebuilts slash commands
├── store/sqlite.py          alert state + current listing catalog
├── watcher.py               orchestrator and automatic daily rankings
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

## Value rankings

Once per UTC day after a watcher cycle, Discord receives up to **10 laptops** and **10 prebuilts**, all priced at or below **$3,000 CAD**. `/laptops` and `/prebuilts` print the same current lists on demand. Entries are intentionally sent from #10 to #1, so deals get worse as you scroll upward from the bottom of a printout.

The auditable value index in `ranking.py` is:

```
estimated fair hardware value / current price × 100
```

Estimated hardware value combines a platform/chassis allowance, a versioned CPU contribution table, an optional GPU contribution table, system RAM, and a condition adjustment (new/open-box/refurbished/used). Unknown or incomplete components reduce confidence. It is a consistent deal-comparison heuristic—not a claim about exact resale value or a live benchmark price feed.

`ranking.max_age_hours`, list limits, and daily automatic posting are configurable in `config.yaml`; code also enforces the hard $3,000 printout cap.

## How alerts work

- **NEW**: previously-unseen `(retailer, sku)` AND price ≤ tier ceiling for that CPU.
- **DROP**: known SKU, current price is `alert_on_drop_pct`% below the last *alerted* price (not last seen — prevents re-firing on small wiggles).
- **None**: silent. Most runs.

State and recently seen complete listing details live in `state.db` (SQLite), committed back to the repo at the end of each cron run. Rankings ignore entries older than the configured freshness window.

## Adding a retailer

1. Add a module under `retailers/`, e.g. `costco_ca.py`.
2. Subclass `Retailer` from `retailers/base.py` and implement `search(cpu_filter) -> list[Listing]`.
3. Register its module/class in `watcher.py`'s `RETAILER_REGISTRY`.
4. Add the key to `retailers:` in `config.yaml`.
