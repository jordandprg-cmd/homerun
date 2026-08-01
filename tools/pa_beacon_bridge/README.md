# PA Beacon → homerun bridge

Reads the Chinese-language **PA Beacon** Telegram forum, translates it, pulls the
wallet out of each alert, scores the wallet, throws away the mediocre ones, and
registers the survivors with homerun's existing smart-money wallet tracker.

Everything is one file: `smart_money_bridge.py`. It lives here in the repo for
convenience, but it is a standalone script — it only ever talks to homerun over
its public HTTP API (`HOMERUN_BASE_URL`, default `http://localhost:8000`).
It does not import any backend code and does not modify homerun.

---

## Install

The bridge has its own two dependencies — deliberately not added to
`backend/requirements*.txt`, since nothing in homerun imports this script:

```bash
pip install telethon httpx
```

From `tools/pa_beacon_bridge/`:

```bash
cp .env.example .env
```

and fill in `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from
https://my.telegram.org → *API development tools*.

## First run

```bash
python smart_money_bridge.py login
```

One-time interactive login (phone number → code → 2FA password if you have one).
Writes `smart_money_bridge.session`; you won't be asked again. Treat that file
like a password — it *is* a logged-in Telegram session. (Gitignored — see
`.gitignore`.)

```bash
python smart_money_bridge.py list-topics
```

Prints the forum's real topic ids. Paste them into `bridge_config.json` →
`telegram.topics[].id`. Not strictly required — the bridge falls back to
matching topics by title — but ids are stable and titles are not.

## Getting the history in

Two ways. **Prefer the export** for the initial load.

### A. Telegram Desktop export (recommended for history)

On whichever PC runs Telegram Desktop: **Settings → Advanced → Export Telegram
data**, select PA Beacon only, format **JSON**, export. Copy the resulting
folder here, then:

```bash
python smart_money_bridge.py import-export --file "C:\path\to\ChatExport_2026-07-31"
```

No API calls, no rate limits, no partial sweep — the whole history in one pass,
and it needs no Telegram session at all. It reads the export's `text_entities`,
so the hyperlink URLs behind "Polymarket 链接" (the only place the leaderboard's
full addresses appear) come through intact.

Add `--all-topics` to ingest every topic rather than just the enabled ones.

### B. Live API sweep

```bash
python smart_money_bridge.py backfill        # sweep history over MTProto
```

Slower, subject to Telegram rate limits, and needs `login` first. Use it for
topping up, or if you'd rather not deal with the export.

Either way the two merge cleanly — messages dedup on `(topic, msg_id)` and
wallets on their key — so you can import the export today and run the live
listener from here on without double-counting.

## What a real import looks like

Measured against the full PA Beacon export (320 MB, 2026-04-08 → 2026-07-31):

| Topic | Messages | Parsed |
|---|---|---|
| 世界杯专区 | 107,592 | 100.0% |
| 聪明钱追踪 | 16,588 | 99.8% |
| 大额买入 | 9,550 | 99.9% |
| 每日榜单 | 590 | 99.2% |
| 讨论组 | 59 | 0% (human chat — correctly ignored) |
| **total** | **134,379** | 34 seconds |

That yields **7,254 distinct wallets**: 5,389 with a full `0x` address, 1,732
resolvable by profile name, and only 133 (1.8%) unresolvable. Tier 1 cuts that
to ~3,135, or ~2,061 with 世界杯专区 left disabled.

**世界杯专区 is disabled by default for a reason.** It is 80% of the messages
and 4,325 of the wallets, all on essentially one market, and none of those
wallets carry published stats. Enable it if you want the breadth; leave it off
for signal density.

## Normal use

```bash
python smart_money_bridge.py score --limit 500   # best 500 by published profit
python smart_money_bridge.py review              # look at what qualified
python smart_money_bridge.py inject --dry-run
python smart_money_bridge.py inject              # push into homerun
```

`score` with no `--limit` evaluates every Tier-1 survivor, which at ~2,000
wallets and two API calls each is a multi-hour run — fine overnight, less fine
when you're still tuning thresholds. Candidates are ordered by the profit PA
Beacon published for them, so `--limit 500` means *the best 500*, not a random
slice. Start there, look at the score distribution, then commit to a full pass.

Once you trust the scores, run the daemon instead — it backfills, listens live,
and re-scores on a timer:

```bash
python smart_money_bridge.py run
```

Set `AUTO_INJECT=true` in `.env` to have the daemon push qualified wallets
without asking.

---

## How it reads PA Beacon

Each topic uses a different template, so each gets its own parser.

| Topic | `kind` | What the address looks like | What gets sent to homerun |
|---|---|---|---|
| 聪明钱追踪 | `smart_money` | `0x448861...99e319（Flipadelphia）` | full address, from the `Profile：` URL |
| 大额买入 | `large_buy` | `0x7b02b2...2a4991（foodenjoyer）` | `foodenjoyer` — no full address is ever printed |
| 世界杯专区 | `worldcup` | `账户：LBmlb / 0xf79c...0bcb96` | `LBmlb` |
| 每日榜单 | `leaderboard` | `4. AppleTime67 / 0xacb206...7eb989` | full address, from the `Polymarket 链接` hyperlink |
| 讨论组 | `ignore` | — | — |

Three details that a naive parser gets wrong:

1. **Addresses are elided in the visible text.** The full 40-hex address exists
   only in the `Profile：` line or behind a hyperlink. The bridge harvests URLs
   from Telegram's *message entities*, not just the message body.
2. **Elided addresses are reconciled over time.** `0x7b02b2...2a4991` seen in
   one topic is matched by prefix/suffix against any full address learned
   anywhere else, and the two candidate records are merged — mentions, feed
   stats and all. That's the `address_index` table.
3. **A message can name several wallets** (the `其他持仓` lines, market URLs).
   The bridge only accepts a full address that actually satisfies the elided
   form printed on the `地址` line. If nothing matches, it falls back to the
   profile name rather than guessing.

When a wallet is only ever printed elided *and* has no display name, it is
recorded but marked `unresolvable` — not eliminated. It's a data gap, not a
quality judgement, and it resolves itself the moment that wallet appears
somewhere with a name or a link.

## How wallets are scored

Two tiers, because the group names thousands of wallets and each API-scored
wallet costs two Polymarket round-trips.

**Tier 1 — free.** PA Beacon already publishes `15天盈利`, `盈利`, `收益率`,
`胜率`, `平均仓位`, and board rank. Tier 1 filters on those alone. A wallet the
bot itself ranked on 稳健盈利榜 / 高胜率榜 skips the profit floor. A wallet
with no published stats at all passes if its biggest reported single trade
clears `min_single_trade_usd` — absence of evidence isn't evidence.

**Tier 2 — the real thing.** Survivors get their positions and trades pulled
*through homerun's own API* (`/api/wallets/{id}/positions` and `/trades`), so
the bridge inherits homerun's rate limiter and username cache instead of
running a second uncoordinated scraper against Polymarket. Then:

| Component | Weight | Measures |
|---|---|---|
| `pnl` | 0.26 | realized + unrealized profit |
| `win_rate` | 0.19 | share of positions in profit (40% → 0, 80% → 1) |
| `lump_size` | 0.17 | p90 position size — the "large lump sums" |
| `volume` | 0.11 | total capital deployed |
| `breadth` | 0.08 | distinct markets — kills one-hit wonders |
| `recency` | 0.07 | days since last trade |
| `group_signal` | 0.06 | how often PA Beacon flags them |
| `feed_quality` | 0.06 | the bot's own pnl / win rate / rank |

Money terms are log-saturating on purpose: a $10M wallet should not score 40×
a $250k one. Past a point, more money stops being more signal.

Then **hard gates**. Failing any one is elimination regardless of score:
min pnl, min positions, min volume, min win rate, max days idle, min score.
All of it is in `bridge_config.json` — nothing is baked into the code.

### Keeping only the best N%

Absolute thresholds are guesswork until you've seen what this group actually
produces. Set `scoring.top_percent` and the gates get followed by a relative
cut — keep the best 10% by score, drop the rest:

```json
"top_percent": 10,
"top_percent_floor": 10
```

The floor stops a small candidate pool collapsing to one wallet. The cut is
**reversible**: loosen `top_percent` and previously-demoted wallets return on
the next `score` run. Wallets already injected into homerun are never demoted —
pulling a wallet back out is your call, not the bridge's. Leave `top_percent`
at `null` to use the absolute gates alone.

## Injection

`POST /api/wallets?address=<identifier>&label=pabeacon-<score>`

homerun's `resolve_wallet_identifier()` accepts a `0x…` address, a bare
username, `@username`, or a `polymarket.com/@user` URL — so the bridge sends
the full address when it has one and the profile name when it doesn't, and
homerun resolves either. Already-tracked wallets are skipped, not duplicated.

From there the wallets flow into homerun's normal machinery: the wallet
tracker, `traders_copy_trade_signal_service`, and the `traders_confluence`
strategy that looks for several tracked wallets converging on one market.

---

## Files

| File | Tracked in git? | |
|---|---|---|
| `smart_money_bridge.py` | yes | the whole bridge |
| `.env.example` | yes | template — copy to `.env` and fill in secrets |
| `.env` | **no** (gitignored) | secrets and machine-specific settings |
| `bridge_config.json` | yes | topics, thresholds, weights — edit freely |
| `samples.txt` | yes | real PA Beacon messages, for `test-parse` |
| `smart_money_bridge.db` | **no** (gitignored) | SQLite state: messages, candidates, scores, address index |
| `smart_money_bridge.session` | **no** (gitignored) | Telegram login — **treat as a credential** |

## Tuning the parser

If PA Beacon changes its templates, or a topic isn't yielding wallets:

```bash
python smart_money_bridge.py dump-unparsed      # messages that extracted nothing
```

Paste a few into `samples.txt` (blocks separated by a line containing only
`---`, optionally prefixed with `#kind: leaderboard`), then:

```bash
python smart_money_bridge.py test-parse --file samples.txt
```

That runs the full translate + extract path with no Telegram, no network and
no database writes.
