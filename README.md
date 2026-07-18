# The Champions League — Trade History

An interactive timeline of every trade in our Sleeper dynasty league
(2021–present). Time runs along the x-axis; every player and pick sits on its
own row. Traded draft picks are shown as the official asset at the time of the
trade — `year + original owner + round` — with the exact draft slot and the
player it became in parentheses (e.g. **2022 SlotLife 1st (1.01 - Bijan
Robinson)**). Dots are colored by who received the asset. Hover (or tap on
mobile) any dot for the full trade; click to pin the details.

## Live page

Once GitHub Pages is enabled (Settings → Pages → Build from branch → `main` /
root), the dashboard is live at:

```
https://<your-username>.github.io/<your-repo>/
```

That's the link to drop in the league chat. It works on desktop and mobile.

## How it stays up to date

A GitHub Action (`.github/workflows/update-trades.yml`) re-pulls from the
Sleeper API and rebuilds the page **every Tuesday**, committing any changes
automatically. You can also trigger it any time from the **Actions** tab →
*Update trade history* → **Run workflow**. No servers, no maintenance.

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
```

## Notes on the data

- The league chain is walked automatically from the current season back to the
  2021 startup, so it keeps working every new season.
- **2021 has two startup drafts on record.** The correct one (Mahomes 1.01) is
  the one Sleeper links to the 2021 league, which is what this uses.
- Picks for the 2026 rookie draft (not yet held) show as **TBD** with their
  projected slot; picks for 2027+ show as **future** picks.

Built from the public [Sleeper API](https://docs.sleeper.com/).
