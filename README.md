# The Champions League — Trade History

An interactive timeline of every trade in our Sleeper dynasty league
(2021–present). Time runs along the x-axis; every player and pick sits on its
own row. Traded draft picks are shown as the official asset at the time of the
trade — `year + original owner + round` — with the exact draft slot and the
player it became in parentheses (e.g. **2022 SlotLife 1st (1.01 - Bijan
Robinson)**). Dots are colored by who received the asset. Hover (or tap on
mobile) any dot for the full trade; click to pin the details.

## Live page

With GitHub Pages enabled (Settings → Pages → Build from branch → `main` /
root), the dashboard is live at:

```
https://mmoyer35.github.io/champions-league-trades/
```

That's the link to drop in the league chat. It works on desktop and mobile.

## How it stays up to date

A GitHub Action (`.github/workflows/update-trades.yml`) re-pulls from the
Sleeper API and rebuilds the page **every Tuesday at 8am ET**, committing any
changes automatically. You can also trigger it any time from the **Actions**
tab → *Update trade history* → **Run workflow**. No servers, no maintenance.

Three things keep a silent failure from looking like a working page:

- **The page states its own age.** Under the headline it reads
  *"Data refreshed Aug 18, 2026, 12:45 PM (5 days ago)"*. Past 8 days it turns
  orange and says the update has not run — so "nothing new" is never confused
  with "nothing is running."
- **The league id rolls forward on its own.** Sleeper mints a new league id
  every season and only ever points *backward*, so a pinned id quietly goes
  stale at rollover. `resolve_latest_league()` walks forward through league
  members to find the current season's league before doing anything else.
- **A bad fetch cannot overwrite a good file.** If the rebuild produces fewer
  trades than `trades.json` already holds, the script aborts and the Action
  goes red instead of committing the loss.

Each run's summary in the Actions tab shows the trade count before and after.

## Files

| File | What it is |
|------|-----------|
| `index.html` / `trade_timeline.html` | The self-contained dashboard (all data baked in — no dependencies). |
| `ff_history.py` | Pulls the full history from Sleeper and rebuilds the dashboard. Standard library only. |
| `trades.json` | The assembled trade data the page is built from. |

## Run it yourself

```bash
python ff_history.py                       # refresh everything from Sleeper
python ff_history.py --from-json trades.json   # just rebuild the page from saved data
python ff_history.py --league <league_id>      # point at a different league
python ff_history.py --allow-shrink            # override the trade-count guard
```

## Notes on the data

- The league chain is resolved forward to the current season, then walked back
  to the 2021 startup, so it keeps working every new season without anyone
  editing the league id.
- **2021 has two startup drafts on record.** The correct one (Mahomes 1.01) is
  the one Sleeper links to the 2021 league, which is what this uses.
- Picks for the 2026 rookie draft (not yet held) show as **TBD** with their
  projected slot; picks for 2027+ show as **future** picks.

Built from the public [Sleeper API](https://docs.sleeper.com/).
