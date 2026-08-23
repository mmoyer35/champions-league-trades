#!/usr/bin/env python3
"""
The Champions League - Sleeper Trade History
=============================================

Pulls the full trade history for a Sleeper dynasty league (walking every
season in the league chain), resolves every traded draft pick to the player
who was actually drafted in that slot, and writes:

    trades.json          - the assembled data model
    trade_timeline.html  - a self-contained interactive dashboard

USAGE
-----
Refresh live from the Sleeper API (run this on your own machine - the Sleeper
API is public and needs no login):

    python ff_history.py

Rebuild the dashboard from an existing trades.json without hitting the API:

    python ff_history.py --from-json trades.json

Point at a different league / starting season:

    python ff_history.py --league 1312051986811609088

Only the Python standard library is required (urllib, json, concurrent).
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

API = "https://api.sleeper.app/v1"
DEFAULT_LEAGUE = "1312051986811609088"  # The Champions League - 2026
TEAMS = 12

# The 2021 season has TWO drafts on record. Draft A (below) is the correct
# startup and is the one linked to the 2021 league object, so using each
# league's own draft_id picks the right one automatically.
CORRECT_2021_DRAFT = "739581558373117952"


# --------------------------------------------------------------------------
# Fetch helpers
# --------------------------------------------------------------------------
def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ff-history/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa
            if i == tries - 1:
                print("  ! failed %s (%s)" % (url, e), file=sys.stderr)
                return None
            time.sleep(1.0 + i)
    return None


def parallel(func, items, workers=12):
    out = [None] * len(items)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(func, it): i for i, it in enumerate(items)}
        for fut in cf.as_completed(futs):
            out[futs[fut]] = fut.result()
    return out


# --------------------------------------------------------------------------
# Build the data model from the API
# --------------------------------------------------------------------------
def resolve_latest_league(start_league):
    """Follow the league chain FORWARD to the newest season.

    Sleeper mints a brand-new league id every season and the league object only
    points backward (`previous_league_id`). A pinned id therefore goes stale the
    moment the league rolls over -- and nothing errors, the script just keeps
    rebuilding an old season forever. Walk forward instead: ask this season's
    members which leagues they are in next season and take the one whose
    previous_league_id is the league we already know.
    """
    state = get("%s/state/nfl" % API) or {}
    try:
        target = int(state.get("league_season") or state.get("season"))
    except (TypeError, ValueError):
        print("  ! could not read current season from Sleeper; using pinned league")
        return start_league

    lid = start_league
    lg = get("%s/league/%s" % (API, lid))
    if not lg:
        return start_league

    while int(lg["season"]) < target:
        nxt = int(lg["season"]) + 1
        found = None
        for u in (get("%s/league/%s/users" % (API, lid)) or [])[:6]:
            uid = u.get("user_id")
            if not uid:
                continue
            for cand in get("%s/user/%s/leagues/nfl/%s" % (API, uid, nxt)) or []:
                if cand.get("previous_league_id") == lid:
                    found = cand
                    break
            if found:
                break
        if not found:
            print("  . no %s league linked to %s yet - staying on %s"
                  % (nxt, lid, lg["season"]))
            break
        lg, lid = found, found["league_id"]
        print("  -> rolled forward to %s league %s" % (lg["season"], lid))

    return lid


def build_model(start_league):
    start_league = resolve_latest_league(start_league)

    # 1. Walk the league chain back to the first season.
    print("Walking league chain from", start_league)
    chain = []
    lid = start_league
    while lid:
        lg = get("%s/league/%s" % (API, lid))
        if not lg:
            break
        chain.append(lg)
        lid = lg.get("previous_league_id")
    chain.reverse()
    seasons = [{"season": lg["season"], "league_id": lg["league_id"],
                "draft_id": lg["draft_id"]} for lg in chain]
    print("  seasons:", ", ".join(s["season"] for s in seasons))

    # 2. Per league: users, rosters, trades (weeks 0-18).
    raw = {}
    for s in seasons:
        L = s["league_id"]
        users = get("%s/league/%s/users" % (API, L)) or []
        rosters = get("%s/league/%s/rosters" % (API, L)) or []
        weeks = parallel(lambda w, L=L: get("%s/league/%s/transactions/%s" % (API, L, w)),
                         list(range(0, 19)))
        tx = []
        for wk in weeks:
            if isinstance(wk, list):
                tx += wk
        trades = [t for t in tx if t.get("type") == "trade"]
        raw[s["season"]] = {"users": users, "rosters": rosters, "trades": trades}
        print("  %s: %d trades" % (s["season"], len(trades)))

    # 3. Per season draft: object (for slot_to_roster_id) + picks.
    drafts = {}
    for s in seasons:
        obj = get("%s/draft/%s" % (API, s["draft_id"]))
        picks = None
        if obj and obj.get("status") == "complete":
            picks = get("%s/draft/%s/picks" % (API, s["draft_id"]))
        drafts[s["season"]] = {"obj": obj, "picks": picks}

    # 4. Identity maps.
    disp, r2u = {}, {}
    for season, d in raw.items():
        disp[season] = {u["user_id"]: u.get("display_name") for u in d["users"]}
        r2u[season] = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}

    def mgr(season, rid):
        return disp[season].get(r2u[season].get(rid)) or ("Roster %s" % rid)

    # 5. Pick resolution: (pickSeason, round, original roster_id) -> drafted player.
    def resolve(pick_season, rnd, orig_rid):
        d = drafts.get(pick_season)
        if not d or not d["obj"]:
            return {"status": "no_draft"}
        s2r = d["obj"].get("slot_to_roster_id") or {}
        slot = None
        for k, v in s2r.items():
            if str(v) == str(orig_rid):
                slot = int(k)
                break
        rounds = (d["obj"].get("settings") or {}).get("rounds", 0)
        if slot is None:
            return {"status": "slot_unknown", "season": pick_season, "round": rnd}
        if rnd > rounds:
            return {"status": "round_oob", "season": pick_season, "round": rnd, "slot": slot}
        if not d["picks"]:
            return {"status": "pending", "season": pick_season, "round": rnd,
                    "slot": slot, "pickNo": (rnd - 1) * TEAMS + slot}
        for p in d["picks"]:
            if p["round"] == rnd and p["draft_slot"] == slot:
                m = p.get("metadata") or {}
                nm = ((m.get("first_name", "") + " " + m.get("last_name", "")).strip())
                return {"status": "ok", "season": pick_season, "round": rnd, "slot": slot,
                        "pickNo": p["pick_no"], "player": nm, "pos": m.get("position")}
        return {"status": "not_found", "season": pick_season, "round": rnd, "slot": slot}

    # 6. Player names for veterans traded (needs the big players file once).
    need = set()
    for d in raw.values():
        for t in d["trades"]:
            for pid in (t.get("adds") or {}):
                need.add(pid)
            for pid in (t.get("drops") or {}):
                need.add(pid)
    print("Fetching player names for %d players ..." % len(need))
    players = get("%s/players/nfl" % API) or {}

    def pshort(pid):
        p = players.get(pid) or {}
        nm = p.get("full_name") or ((p.get("first_name", "") + " " + p.get("last_name", "")).strip())
        return nm or ("Player %s" % pid)

    def ppos(pid):
        return (players.get(pid) or {}).get("position", "")

    # 7. Assemble trades.
    trades = []
    for season, d in raw.items():
        for t in d["trades"]:
            items = []
            adds = t.get("adds") or {}
            drops = t.get("drops") or {}
            for pid, to in adds.items():
                frm = drops.get(pid)
                items.append({"kind": "player", "pid": pid, "name": pshort(pid),
                              "pos": ppos(pid), "to": mgr(season, to), "toRid": to,
                              "from": mgr(season, frm) if frm is not None else None})
            for dp in (t.get("draft_picks") or []):
                items.append({"kind": "pick", "pickSeason": dp["season"], "round": dp["round"],
                              "origOwner": mgr(season, dp["roster_id"]),  # whose pick it originally was
                              "to": mgr(season, dp["owner_id"]), "toRid": dp["owner_id"],
                              "from": mgr(season, dp["previous_owner_id"]) if dp.get("previous_owner_id") is not None else None,
                              "resolved": resolve(dp["season"], dp["round"], dp["roster_id"])})
            for w in (t.get("waiver_budget") or []):
                items.append({"kind": "faab", "amount": w["amount"],
                              "to": mgr(season, w["receiver"]), "from": mgr(season, w["sender"])})
            parts = list(dict.fromkeys(mgr(season, rid) for rid in (t.get("roster_ids") or [])))
            trades.append({"id": t["transaction_id"], "season": season, "leg": t.get("leg"),
                           "ts": t["created"], "status": t.get("status"),
                           "managers": parts, "items": items})
    trades.sort(key=lambda x: x["ts"])
    return {"generated": int(time.time() * 1000), "teams": TEAMS,
            "tradeCount": len(trades), "trades": trades}


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def build_html(model, out_path):
    html = HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(model, separators=(",", ":")))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote", out_path)


def main():
    ap = argparse.ArgumentParser(description="Sleeper league trade history + dashboard")
    ap.add_argument("--league", default=DEFAULT_LEAGUE, help="starting (latest) league id")
    ap.add_argument("--from-json", dest="from_json", default=None,
                    help="skip the API and build the dashboard from this trades.json")
    ap.add_argument("--outdir", default=".", help="output directory")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit writing fewer trades than the existing trades.json")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    json_path = os.path.join(args.outdir, "trades.json")
    html_path = os.path.join(args.outdir, "trade_timeline.html")

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            model = json.load(f)
        print("Loaded %d trades from %s" % (model.get("tradeCount", 0), args.from_json))
    else:
        model = build_model(args.league)
        prev = None
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                prev = None
        if prev and not args.allow_shrink and model["tradeCount"] < prev.get("tradeCount", 0):
            sys.exit("ABORT: rebuilt %d trades but %s already holds %d. A partial "
                     "Sleeper fetch would overwrite good data with bad. Re-run, or "
                     "pass --allow-shrink if the drop is genuine."
                     % (model["tradeCount"], json_path, prev.get("tradeCount", 0)))
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(model, f, separators=(",", ":"))
        print("Wrote", json_path)

    build_html(model, html_path)
    print("Done. Open %s in your browser." % html_path)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Champions League — Trade History</title>
<style>
  :root{
    --bg:#0d1017; --panel:#151a23; --panel2:#1b2230; --line:#252d3a;
    --ink:#e7ecf3; --dim:#93a1b5; --faint:#5c6b80; --accent:#5B8FF9;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--ink);
    font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  header{padding:18px 22px 12px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:20px;letter-spacing:.2px}
  .sub{color:var(--dim);font-size:13px;margin-top:3px}
  .sub.stale{color:#f0a04b;font-weight:600}
  .wrap{display:flex;flex-direction:column;height:100vh}
  .controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;
    padding:11px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
  .grp{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .grp .lbl{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.7px;margin-right:2px}
  .chip{cursor:pointer;user-select:none;border:1px solid var(--line);background:var(--panel2);
    color:var(--dim);padding:4px 10px;border-radius:20px;font-size:12px;transition:.12s}
  .chip:hover{border-color:#3a4759;color:var(--ink)}
  .chip.on{background:var(--accent);border-color:var(--accent);color:#06101f;font-weight:600}
  .toggle{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim);cursor:pointer}
  .toggle input{accent-color:var(--accent)}
  input.search{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;padding:5px 9px;font-size:12px;width:150px}
  input.search:focus{outline:none;border-color:var(--accent)}
  .zoombtn{cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--dim);
    width:26px;height:26px;border-radius:6px;font-size:15px;line-height:1}
  .zoombtn:hover{color:var(--ink);border-color:#3a4759}
  .legend{display:flex;flex-wrap:wrap;gap:5px;padding:9px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
  .lchip{display:flex;align-items:center;gap:6px;cursor:pointer;border:1px solid var(--line);
    background:var(--panel2);border-radius:16px;padding:3px 9px 3px 6px;font-size:12px;color:var(--dim);transition:.12s}
  .lchip:hover{color:var(--ink)}
  .lchip.on{color:var(--ink);border-color:#3a4759;background:#212a39}
  .lchip.off{opacity:.38}
  .sw{width:11px;height:11px;border-radius:50%}
  .main{flex:1;display:flex;min-height:0}
  .chart{flex:1;display:flex;flex-direction:column;min-width:0}
  .axis{overflow:hidden;border-bottom:1px solid var(--line);background:var(--bg)}
  .body{overflow:auto;flex:1}
  .body::-webkit-scrollbar{width:11px;height:11px}
  .body::-webkit-scrollbar-thumb{background:#2a3342;border-radius:6px}
  .body::-webkit-scrollbar-track{background:transparent}
  svg{display:block}
  text{fill:var(--ink)}
  .rowlabel{font-size:11px;fill:var(--dim)}
  .rowlabel.hl{fill:var(--ink);font-weight:600}
  .rowpos{font-size:9px;fill:var(--faint)}
  .gridline{stroke:var(--line);stroke-width:1}
  .seasonband{fill:#ffffff03}
  .rowsep{stroke:#ffffff06;stroke-width:1}
  .rowband{fill:var(--accent);opacity:0}
  .axtick{font-size:11px;fill:var(--dim)}
  .axseason{font-size:12px;fill:var(--ink);font-weight:600}
  .conn{stroke:var(--ink);stroke-width:1.4;opacity:.55}
  .dot{cursor:pointer}
  .panel{width:340px;flex:none;border-left:1px solid var(--line);background:var(--panel);
    overflow:auto;padding:0}
  .panel.hidden{display:none}
  .phead{padding:14px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
  .phead .dt{color:var(--dim);font-size:12px}
  .phead .mg{font-size:15px;font-weight:600;margin-top:2px}
  .pclose{float:right;cursor:pointer;color:var(--faint);font-size:18px;line-height:1}
  .pclose:hover{color:var(--ink)}
  .side{padding:12px 16px;border-bottom:1px solid var(--line)}
  .side h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;
    display:flex;align-items:center;gap:7px}
  .asset{display:flex;gap:8px;align-items:flex-start;padding:4px 0;font-size:13px}
  .asset .ic{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:none}
  .asset .meta{color:var(--faint);font-size:11px}
  .empty{padding:40px 20px;color:var(--faint);text-align:center;font-size:13px}
  .tip{position:fixed;pointer-events:none;z-index:50;max-width:320px;background:#0a0e15ee;
    border:1px solid #33404f;border-radius:8px;padding:9px 11px;font-size:12px;color:var(--ink);
    box-shadow:0 8px 26px #000a;opacity:0;transition:opacity .08s}
  .tip .tdt{color:var(--dim);font-size:11px;margin-bottom:4px}
  .tip .trow{display:flex;gap:6px;align-items:baseline;margin:2px 0}
  .tip .arrow{color:var(--faint)}
  .tip b{font-weight:600}
  .hint{color:var(--faint);font-size:11px;margin-left:auto}
  .foot{padding:7px 22px;border-top:1px solid var(--line);color:var(--faint);font-size:11px;
    display:flex;gap:16px;flex-wrap:wrap;background:var(--panel)}
  @media(max-width:640px){
    header{padding:11px 13px 7px} h1{font-size:16px} .sub{font-size:11px}
    .controls,.legend{padding-left:12px;padding-right:12px;gap:9px}
    .hint{display:none}
    .panel{position:fixed;inset:0;width:100%;z-index:60}
    .foot{padding:7px 12px}
  }
  .badge{display:inline-block;font-size:9px;padding:1px 5px;border-radius:4px;
    background:#2a3342;color:var(--dim);vertical-align:middle;margin-left:5px;letter-spacing:.4px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>The Champions League <span style="color:var(--faint);font-weight:400">· Trade History</span></h1>
    <div class="sub" id="sub"></div>
    <div class="sub" id="freshness"></div>
  </header>

  <div class="controls">
    <div class="grp" id="seasonChips"><span class="lbl">Season</span></div>
    <div class="grp">
      <span class="lbl">Show</span>
      <label class="toggle"><input type="checkbox" id="tPlayer" checked> Players</label>
      <label class="toggle"><input type="checkbox" id="tPick" checked> Picks</label>
      <label class="toggle"><input type="checkbox" id="tFaab"> FAAB</label>
    </div>
    <div class="grp">
      <span class="lbl">Player</span>
      <input class="search" id="search" placeholder="filter by name…">
    </div>
    <div class="grp">
      <span class="lbl">Sort</span>
      <select id="sort" class="search" style="width:auto">
        <option value="time">First traded</option>
        <option value="name">Name A–Z</option>
        <option value="pos">Position</option>
      </select>
    </div>
    <div class="grp">
      <span class="lbl">Zoom</span>
      <button class="zoombtn" id="zOut">−</button>
      <button class="zoombtn" id="zIn">+</button>
      <button class="zoombtn" id="zReset" style="width:auto;padding:0 8px;font-size:11px">reset</button>
    </div>
    <span class="hint">hover a dot for the trade · click to pin details</span>
  </div>

  <div class="legend" id="legend"></div>

  <div class="main">
    <div class="chart">
      <div class="axis"><svg id="axisSvg" height="34"></svg></div>
      <div class="body" id="body"><svg id="plot"></svg></div>
    </div>
    <div class="panel hidden" id="panel"></div>
  </div>

  <div class="foot" id="foot"></div>
</div>
<div class="tip" id="tip"></div>

<script>
const MODEL = /*__DATA__*/;
const TRADES = MODEL.trades;

// ---- managers + colors -------------------------------------------------
const PALETTE = ['#5B8FF9','#5AD8A6','#F6BD16','#E8684A','#6DC8EC','#9270CA',
                 '#FF9D4D','#269A99','#FF99C3','#A5D64C','#B07AA1','#8B6F47'];
const managers = [...new Set(TRADES.flatMap(t=>t.managers))].sort();
const color = {};
managers.forEach((m,i)=>color[m]=PALETTE[i%PALETTE.length]);

// ---- ordinal + pick label ---------------------------------------------
function ord(n){return n+({1:'st',2:'nd',3:'rd'}[n]||'th');}
function pad2(n){return String(n).padStart(2,'0');}
// the pick exactly as it was named at trade time: "<year> <original owner> <round>"
// e.g. "2027 mmoyer35 1st"  (dynasty picks are known by whose pick it originally was)
function pickCode(it){
  const owner = it.origOwner ? (' '+it.origOwner) : '';
  return it.pickSeason+owner+' '+ord(it.round);
}
// the exact draft slot in "round.pick" notation, e.g. "1.03" (null if not drafted / no order yet)
function pickSlot(it){ const r=it.resolved||{}; return r.slot ? (it.round+'.'+pad2(r.slot)) : null; }
// official asset label: the pick, then (slot - player it became) in parentheses
function pickOfficial(it){
  const r=it.resolved||{};
  if(r.status==='ok')      return pickCode(it)+' ('+pickSlot(it)+' - '+r.player+')';
  if(r.status==='pending') return pickCode(it)+' ('+pickSlot(it)+' - TBD)';
  return pickCode(it)+' (future)';
}

// ---- build events + rows ----------------------------------------------
// Each asset that changed hands becomes an event on a player/asset "row".
// Each traded pick keeps its own identity (the official asset). All trades of
// the SAME physical pick share a row; the label shows the player it became.
function rowOf(it){
  if(it.kind==='player') return {key:'N:'+it.name, label:it.name, pos:it.pos||'', marker:'player'};
  if(it.kind==='faab')   return {key:'FAAB', label:'FAAB (cash)', pos:'$', marker:'faab'};
  const r=it.resolved||{};
  if(r.status==='ok')      return {key:'PK:'+r.season+':'+r.pickNo, label:pickOfficial(it), pos:'PICK', marker:'pick'};
  if(r.status==='pending') return {key:'PK:'+r.season+':'+r.pickNo, label:pickOfficial(it), pos:'PICK', marker:'pending'};
  return {key:'FUT:'+it.pickSeason+':'+(it.origOwner||'')+':R'+it.round, label:pickOfficial(it), pos:'FUT', marker:'future'};
}

const rowMap = new Map();
const events = [];
TRADES.forEach((t,ti)=>{
  t.items.forEach(it=>{
    const r=rowOf(it);
    if(!rowMap.has(r.key)) rowMap.set(r.key,{key:r.key,label:r.label,pos:r.pos,firstTs:t.ts,marker:r.marker});
    else rowMap.get(r.key).firstTs=Math.min(rowMap.get(r.key).firstTs,t.ts);
    let desc, sub=null;
    if(it.kind==='player'){ desc=it.name; sub=it.pos; }
    else if(it.kind==='faab'){ desc='$'+it.amount+' FAAB'; }
    else {
      const rr=it.resolved||{};
      if(rr.status==='ok'){ desc=pickCode(it)+' ('+rr.player+')'; }
      else if(rr.status==='pending'){ desc=pickCode(it)+' (TBD)'; }
      else { desc=it.pickSeason+' '+ord(it.round)+' pick (future)'; }
    }
    events.push({ti, ts:t.ts, season:t.season, rowKey:r.key, marker:r.marker,
      cat: it.kind==='faab'?'faab':(it.kind==='pick'?'pick':'player'),
      to:it.to, from:it.from, desc, sub});
  });
});

// ---- time scale --------------------------------------------------------
const tsAll = events.map(e=>e.ts);
const tMin = Math.min(...tsAll), tMax = Math.max(...tsAll);
const pad = (tMax-tMin)*0.03;
const domainMin = tMin-pad, domainMax = tMax+pad;
const M_RIGHT=24, ROWH=16, DOT=4.2;  // M_LEFT is responsive (set per render)
let zoom = 1;

// ---- state -------------------------------------------------------------
const seasons = [...new Set(TRADES.map(t=>t.season))].sort();
const state = {
  season:'all',
  showPlayer:true, showPick:true, showFaab:false,
  search:'', sort:'time',
  isolate:new Set(),        // managers isolated (empty = all)
};

// ---- DOM refs ----------------------------------------------------------
const $=id=>document.getElementById(id);
const plot=$('plot'), axisSvg=$('axisSvg'), body=$('body'), tip=$('tip');
const NS='http://www.w3.org/2000/svg';
function el(n,a){const e=document.createElementNS(NS,n);for(const k in(a||{}))e.setAttribute(k,a[k]);return e;}

// ---- header / controls -------------------------------------------------
(function initHeader(){
  const nP=events.filter(e=>e.cat==='player').length;
  const nPick=events.filter(e=>e.cat==='pick').length;
  const nResolved=TRADES.flatMap(t=>t.items).filter(it=>it.kind==='pick'&&it.resolved&&it.resolved.status==='ok').length;
  $('sub').textContent = TRADES.length+' trades · '+managers.length+' managers · '
    + seasons[0]+'–'+seasons[seasons.length-1]+'  ·  '+nP+' players & '+nPick+' picks moved ('
    + nResolved+' traded picks resolved to the player actually drafted)';
  // Freshness stamp: makes "the update stopped running" visually distinct
  // from "nobody has traded lately" -- previously indistinguishable.
  (function(){
    const el=$('freshness'); if(!el) return;
    if(!MODEL.generated){ el.textContent='Refresh time unknown'; return; }
    const days=(Date.now()-MODEL.generated)/86400000;
    const stamp=new Date(MODEL.generated).toLocaleString(undefined,
      {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
    const ago = days<1 ? Math.max(1,Math.round(days*24))+'h ago'
                       : Math.round(days)+' day'+(Math.round(days)===1?'':'s')+' ago';
    el.textContent='Data refreshed '+stamp+' ('+ago+')';
    el.className='sub';
    if(days>8){ el.className='sub stale';
      el.textContent+=' \u2014 the weekly update has not run, check the Actions tab'; }
  })();
  // season chips
  const sc=$('seasonChips');
  const mk=(val,txt)=>{const c=document.createElement('div');c.className='chip'+(state.season===val?' on':'');
    c.textContent=txt;c.onclick=()=>{state.season=val;[...sc.querySelectorAll('.chip')].forEach(x=>x.classList.remove('on'));c.classList.add('on');render();};sc.appendChild(c);};
  mk('all','All');
  seasons.forEach(s=>mk(s,s));
  // legend
  const lg=$('legend');
  managers.forEach(m=>{
    const c=document.createElement('div');c.className='lchip on';c.dataset.m=m;
    c.innerHTML='<span class="sw" style="background:'+color[m]+'"></span>'+m;
    c.onclick=()=>{ if(state.isolate.has(m))state.isolate.delete(m); else state.isolate.add(m); updateLegend(); render(); };
    lg.appendChild(c);
  });
  // toggles
  $('tPlayer').onchange=e=>{state.showPlayer=e.target.checked;render();};
  $('tPick').onchange=e=>{state.showPick=e.target.checked;render();};
  $('tFaab').onchange=e=>{state.showFaab=e.target.checked;render();};
  $('search').oninput=e=>{state.search=e.target.value.toLowerCase();render();};
  $('sort').onchange=e=>{state.sort=e.target.value;render();};
  $('zIn').onclick=()=>{zoom=Math.min(zoom*1.5,10);render();};
  $('zOut').onclick=()=>{zoom=Math.max(zoom/1.5,1);render();};
  $('zReset').onclick=()=>{zoom=1;render();};
  // sync axis horizontal scroll with body
  body.addEventListener('scroll',()=>{ axisSvg.parentElement.scrollLeft=body.scrollLeft; });
})();
function updateLegend(){
  document.querySelectorAll('.lchip').forEach(c=>{
    const m=c.dataset.m;
    if(state.isolate.size===0){c.classList.remove('off');c.classList.add('on');}
    else{ if(state.isolate.has(m)){c.classList.add('on');c.classList.remove('off');}
      else{c.classList.remove('on');c.classList.add('off');} }
  });
}

// ---- filtering ---------------------------------------------------------
function visibleEvents(){
  return events.filter(e=>{
    if(state.season!=='all' && e.season!==state.season) return false;
    if(e.cat==='player' && !state.showPlayer) return false;
    if(e.cat==='pick'   && !state.showPick)   return false;
    if(e.cat==='faab'   && !state.showFaab)   return false;
    if(state.isolate.size){ if(!state.isolate.has(e.to) && !state.isolate.has(e.from)) return false; }
    if(state.search){ const row=rowMap.get(e.rowKey); if(!row.label.toLowerCase().includes(state.search)) return false; }
    return true;
  });
}

// ---- render ------------------------------------------------------------
let xScale;
function render(){
  const vis=visibleEvents();
  // rows present
  const keys=[...new Set(vis.map(e=>e.rowKey))];
  let rows=keys.map(k=>rowMap.get(k));
  const posOrder={QB:0,RB:1,WR:2,TE:3,K:4,DEF:5,PICK:8,FUT:8,'$':9,'':7};
  rows.sort((a,b)=>{
    if(state.sort==='name') return a.label.localeCompare(b.label);
    if(state.sort==='pos'){ const d=(posOrder[a.pos]??7)-(posOrder[b.pos]??7); return d||a.label.localeCompare(b.label);}
    return a.firstTs-b.firstTs || a.label.localeCompare(b.label);
  });
  const rowIndex=new Map(); rows.forEach((r,i)=>rowIndex.set(r.key,i));

  const cw=body.clientWidth||900;
  const narrow = cw < 640;
  const M_LEFT = narrow ? 150 : 330;          // narrower label gutter on phones
  const TRUNC  = narrow ? 20 : 52;
  const minTime = 560;                         // never let the timeline compress below this
  const plotW=Math.max(cw, M_LEFT+minTime+M_RIGHT)*zoom;  // on mobile the body scrolls sideways
  const innerR=plotW-M_RIGHT;
  xScale=ts=>M_LEFT+ (innerR-M_LEFT)*( (ts-domainMin)/(domainMax-domainMin) );
  const H=Math.max(rows.length*ROWH+12, body.clientHeight-2);

  // ---- axis ----
  while(axisSvg.firstChild)axisSvg.removeChild(axisSvg.firstChild);
  axisSvg.setAttribute('width',plotW); axisSvg.setAttribute('height',34);
  // year ticks
  const y0=new Date(domainMin).getFullYear(), y1=new Date(domainMax).getFullYear();
  for(let y=y0;y<=y1+1;y++){
    const t=new Date(y,0,1).getTime();
    if(t<domainMin||t>domainMax)continue;
    const x=xScale(t);
    axisSvg.appendChild(el('line',{x1:x,y1:20,x2:x,y2:34,stroke:'#33404f'}));
    const tx=el('text',{x:x+4,y:31,class:'axtick'}); tx.textContent=y; axisSvg.appendChild(tx);
  }
  // season labels centered on each season's own event span
  seasons.forEach(s=>{
    const st=TRADES.filter(t=>t.season===s).map(t=>t.ts);
    if(!st.length)return;
    const mid=(Math.min(...st)+Math.max(...st))/2;
    const tx=el('text',{x:xScale(mid),y:14,class:'axseason','text-anchor':'middle'});
    tx.textContent=s+' season'; axisSvg.appendChild(tx);
  });

  // ---- plot ----
  while(plot.firstChild)plot.removeChild(plot.firstChild);
  plot.setAttribute('width',plotW); plot.setAttribute('height',H);

  if(!rows.length){
    const t=el('text',{x:M_LEFT,y:60,fill:'#5c6b80'}); t.textContent='No trades match these filters.';
    plot.appendChild(t); $('foot').textContent=''; return;
  }

  // year gridlines
  for(let y=y0;y<=y1+1;y++){
    const t=new Date(y,0,1).getTime(); if(t<domainMin||t>domainMax)continue;
    plot.appendChild(el('line',{x1:xScale(t),y1:0,x2:xScale(t),y2:H,class:'gridline'}));
  }
  // row bands + labels
  rows.forEach((r,i)=>{
    const cy=i*ROWH+ROWH/2;
    if(i%2) plot.appendChild(el('rect',{x:M_LEFT-6,y:i*ROWH,width:innerR-M_LEFT+6,height:ROWH,fill:'#ffffff04'}));
    const band=el('rect',{x:0,y:i*ROWH,width:plotW,height:ROWH,class:'rowband'});
    band.dataset.row=r.key; plot.appendChild(band);
    const lab=el('text',{x:8,y:cy+3.5,class:'rowlabel'}); lab.dataset.row=r.key;
    lab.textContent = r.label.length>TRUNC?r.label.slice(0,TRUNC-1)+'…':r.label;
    plot.appendChild(lab);
    if(r.pos){ const pl=el('text',{x:M_LEFT-10,y:cy+3.5,class:'rowpos','text-anchor':'end'}); pl.textContent=r.pos; plot.appendChild(pl); }
  });

  // dots
  const byTrade={};
  vis.forEach(e=>{
    const i=rowIndex.get(e.rowKey); const cy=i*ROWH+ROWH/2; const cx=xScale(e.ts);
    (byTrade[e.ti]=byTrade[e.ti]||[]).push({e,cx,cy});
    const c=color[e.to]||'#888';
    let node;
    if(e.marker==='pick'){ // diamond = pick that became a player
      node=el('rect',{x:cx-DOT,y:cy-DOT,width:DOT*2,height:DOT*2,fill:c,transform:'rotate(45 '+cx+' '+cy+')',class:'dot'});
    }else if(e.marker==='pending'){ node=el('circle',{cx,cy,r:DOT,fill:'none',stroke:c,'stroke-width':1.5,class:'dot'});
    }else if(e.marker==='future'){ node=el('circle',{cx,cy,r:DOT-1,fill:'#4a5666',class:'dot'});
    }else if(e.marker==='faab'){ node=el('rect',{x:cx-DOT+.5,y:cy-DOT+.5,width:DOT*2-1,height:DOT*2-1,fill:c,class:'dot'});
    }else{ node=el('circle',{cx,cy,r:DOT,fill:c,class:'dot'}); }
    node.dataset.ti=e.ti; node.dataset.row=e.rowKey;
    node.addEventListener('mousemove',ev=>showTip(ev,e.ti));
    node.addEventListener('mouseleave',hideTip);
    node.addEventListener('click',()=>{openPanel(e.ti);highlightTrade(e.ti,true);});
    plot.appendChild(node);
  });

  window.__byTrade=byTrade; window.__rowIndex=rowIndex;
  $('foot').innerHTML =
    (narrow?'<span style="color:var(--accent)">← swipe sideways for later seasons · tap a dot for the trade</span>':'')
    +'<span>● players</span><span>◆ pick → drafted player</span>'
    +'<span>○ pick (2026, not drafted yet)</span><span>▪ future pick</span>'
    +'<span style="margin-left:auto">'+rows.length+' player rows · '+vis.length+' asset moves shown</span>';
}

// ---- highlight a whole trade (connector + outlines) --------------------
let hlLayer=null;
function highlightTrade(ti, pin){
  clearHl();
  const pts=(window.__byTrade[ti]||[]).slice().sort((a,b)=>a.cy-b.cy);
  if(!pts.length)return;
  hlLayer=el('g',{});
  const x=pts[0].cx;
  if(pts.length>1) hlLayer.appendChild(el('line',{x1:x,y1:pts[0].cy,x2:x,y2:pts[pts.length-1].cy,class:'conn'}));
  pts.forEach(p=>hlLayer.appendChild(el('circle',{cx:p.cx,cy:p.cy,r:DOT+3,fill:'none',stroke:'#e7ecf3','stroke-width':1.3})));
  plot.appendChild(hlLayer);
  // emphasize the involved row labels
  const rowsInv=new Set(pts.map(p=>p.e.rowKey));
  plot.querySelectorAll('text.rowlabel').forEach(l=>{ if(rowsInv.has(l.dataset.row))l.classList.add('hl'); });
}
function clearHl(){ if(hlLayer){hlLayer.remove();hlLayer=null;} plot.querySelectorAll('text.rowlabel.hl').forEach(l=>l.classList.remove('hl')); }

// ---- tooltip -----------------------------------------------------------
function tradeSummaryHTML(ti){
  const t=TRADES[ti];
  const d=new Date(t.ts);
  const perMgr={};
  t.managers.forEach(m=>perMgr[m]=[]);
  t.items.forEach(it=>{ if(it.to&&perMgr[it.to]) perMgr[it.to].push(it); else if(it.to){perMgr[it.to]=[it];} });
  let h='<div class="tdt">'+d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'})
    +' · '+t.season+' season'+(t.leg?(' · wk '+t.leg):'')+'</div>';
  Object.keys(perMgr).forEach(m=>{
    h+='<div class="trow"><span class="sw" style="background:'+(color[m]||'#888')+'"></span><b>'+m+'</b> gets:</div>';
    perMgr[m].forEach(it=>{ h+='<div style="margin-left:16px">'+assetText(it)+'</div>'; });
    if(!perMgr[m].length) h+='<div style="margin-left:16px;color:var(--faint)">—</div>';
  });
  return h;
}
function assetText(it){
  if(it.kind==='player') return it.name+' <span class="arrow">'+(it.pos||'')+'</span>';
  if(it.kind==='faab') return '$'+it.amount+' FAAB';
  const r=it.resolved||{};
  if(r.status==='ok') return '<b>'+pickCode(it)+'</b> <span class="arrow">('+pickSlot(it)+' - '+r.player+')</span>';
  if(r.status==='pending') return '<b>'+pickCode(it)+'</b> <span class="badge">'+pickSlot(it)+' · TBD</span>';
  return '<b>'+pickCode(it)+'</b> <span class="badge">future</span>';
}
function showTip(ev,ti){
  highlightTrade(ti,false);
  tip.innerHTML=tradeSummaryHTML(ti);
  tip.style.opacity=1;
  const pad=14; let x=ev.clientX+pad, y=ev.clientY+pad;
  const r=tip.getBoundingClientRect();
  if(x+r.width>window.innerWidth-8) x=ev.clientX-r.width-pad;
  if(y+r.height>window.innerHeight-8) y=ev.clientY-r.height-pad;
  tip.style.left=x+'px'; tip.style.top=y+'px';
}
function hideTip(){ tip.style.opacity=0; if(!panelPinned)clearHl(); }

// ---- detail panel ------------------------------------------------------
let panelPinned=false;
function openPanel(ti){
  panelPinned=true;
  const t=TRADES[ti]; const p=$('panel'); p.classList.remove('hidden');
  const d=new Date(t.ts);
  const perMgr={}; t.managers.forEach(m=>perMgr[m]=[]);
  t.items.forEach(it=>{ if(it.to){ (perMgr[it.to]=perMgr[it.to]||[]).push(it);} });
  let h='<div class="phead"><span class="pclose" onclick="closePanel()">×</span>'
    +'<div class="dt">'+d.toLocaleString(undefined,{year:'numeric',month:'short',day:'numeric'})
    +' · '+t.season+' season'+(t.leg?(' · week '+t.leg):'')+'</div>'
    +'<div class="mg">'+t.managers.join('  ⇄  ')+'</div></div>';
  Object.keys(perMgr).forEach(m=>{
    h+='<div class="side"><h3><span class="sw" style="background:'+(color[m]||'#888')+'"></span>'+m+' receives</h3>';
    if(!perMgr[m].length) h+='<div class="empty" style="padding:6px 0;text-align:left">—</div>';
    perMgr[m].forEach(it=>{
      const c=color[m]||'#888';
      let main, meta='';
      if(it.kind==='player'){ main=it.name; meta=(it.pos||'')+(it.from?(' · from '+it.from):''); }
      else if(it.kind==='faab'){ main='$'+it.amount+' FAAB'; meta=it.from?('from '+it.from):''; }
      else { const r=it.resolved||{};
        if(r.status==='ok'){ main=pickCode(it)+' ('+pickSlot(it)+' - '+r.player+')'; meta=(r.pos||'')+' · pick '+r.pickNo+' overall'+(it.from?(' · from '+it.from):''); }
        else if(r.status==='pending'){ main=pickCode(it)+' ('+pickSlot(it)+' - TBD)'; meta='not drafted yet'+(it.from?(' · from '+it.from):''); }
        else { main=pickCode(it); meta='future pick'+(it.from?(' · from '+it.from):''); }
      }
      h+='<div class="asset"><span class="ic" style="background:'+c+'"></span><div><div>'+main+'</div><div class="meta">'+meta+'</div></div></div>';
    });
    h+='</div>';
  });
  p.innerHTML=h;
  highlightTrade(ti,true);
}
function closePanel(){ panelPinned=false; $('panel').classList.add('hidden'); clearHl(); }
window.closePanel=closePanel;

window.addEventListener('resize',()=>render());
render();
updateLegend();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
