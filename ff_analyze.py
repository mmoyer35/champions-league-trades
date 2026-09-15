#!/usr/bin/env python3
"""
The Champions League - all-time record book.

Pulls every regular-season and postseason matchup Sleeper has on record for the
league chain (2021 -> current season) and builds a self-contained dashboard:
closest games, blowouts, scoring records, per-manager career lines, an all-time
head-to-head matrix and title history.

The league id is resolved forward every run by ff_history.resolve_latest_league,
so this keeps working across season rollovers without anyone editing a constant.

  python ff_analyze.py                      # refresh everything from Sleeper
  python ff_analyze.py --from-json records.json   # rebuild the page from saved data
"""

import argparse
import json
import os
import sys
import time

from ff_history import API, DEFAULT_LEAGUE, get, parallel, resolve_latest_league

MAX_WEEK = 18


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
def build_model(start_league):
    start_league = resolve_latest_league(start_league)

    print("Walking league chain from", start_league)
    chain, lid = [], start_league
    while lid:
        lg = get("%s/league/%s" % (API, lid))
        if not lg:
            break
        chain.append(lg)
        lid = lg.get("previous_league_id")
    chain.reverse()
    if not chain:
        sys.exit("ABORT: could not read any league from Sleeper.")
    print("  seasons:", ", ".join(lg["season"] for lg in chain))

    games, champions, managers = [], {}, set()

    for lg in chain:
        season, L = lg["season"], lg["league_id"]
        pw = (lg.get("settings") or {}).get("playoff_week_start") or 15

        users = get("%s/league/%s/users" % (API, L)) or []
        rosters = get("%s/league/%s/rosters" % (API, L)) or []
        u2n = {u["user_id"]: (u.get("display_name") or "user %s" % u["user_id"])
               for u in users}
        r2n = {r["roster_id"]: u2n.get(r.get("owner_id"), "Roster %s" % r["roster_id"])
               for r in rosters}
        managers.update(r2n.values())

        weeks = parallel(lambda w, L=L: get("%s/league/%s/matchups/%s" % (API, L, w)),
                         list(range(1, MAX_WEEK + 1)))

        season_games = 0
        for i, wk in enumerate(weeks):
            week = i + 1
            if not isinstance(wk, list):
                continue
            pairs = {}
            for m in wk:
                if m.get("matchup_id") is None:
                    continue
                pairs.setdefault(m["matchup_id"], []).append(m)
            for pair in pairs.values():
                if len(pair) != 2:
                    continue
                a, b = pair
                ap, bp = float(a.get("points") or 0), float(b.get("points") or 0)
                if ap == 0 and bp == 0:
                    continue                      # not played yet
                hi, lo = (a, b) if ap >= bp else (b, a)
                hp, lp = max(ap, bp), min(ap, bp)
                games.append({
                    "s": season, "w": week, "p": 1 if week >= pw else 0,
                    "W": r2n.get(hi["roster_id"], "?"), "wp": round(hp, 2),
                    "L": r2n.get(lo["roster_id"], "?"), "lp": round(lp, 2),
                    "tie": 1 if hp == lp else 0,
                })
                season_games += 1

        bracket = get("%s/league/%s/winners_bracket" % (API, L)) or []
        final = next((m for m in bracket if m.get("p") == 1 and m.get("w") is not None), None)
        if final:
            champions[season] = r2n.get(final["w"], "?")

        print("  %s: %d games%s" % (season, season_games,
                                    ", champion " + champions[season] if season in champions else ""))

    games.sort(key=lambda g: (g["s"], g["w"]))
    return {
        "generated": int(time.time() * 1000),
        "seasons": [lg["season"] for lg in chain],
        "managers": sorted(managers),
        "champions": champions,
        "gameCount": len(games),
        "games": games,
    }


def build_html(model, out_path):
    html = HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(model, separators=(",", ":")))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote", out_path)


def main():
    ap = argparse.ArgumentParser(description="Sleeper league all-time record book")
    ap.add_argument("--league", default=DEFAULT_LEAGUE, help="starting (latest) league id")
    ap.add_argument("--from-json", dest="from_json", default=None,
                    help="skip the API and build the page from this records.json")
    ap.add_argument("--outdir", default=".", help="output directory")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit writing fewer games than the existing records.json")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    json_path = os.path.join(args.outdir, "records.json")
    html_path = os.path.join(args.outdir, "records.html")

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            model = json.load(f)
        print("Loaded %d games from %s" % (model.get("gameCount", 0), args.from_json))
    else:
        model = build_model(args.league)
        prev = None
        if os.path.exists(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                prev = None
        if prev and not args.allow_shrink and model["gameCount"] < prev.get("gameCount", 0):
            sys.exit("ABORT: rebuilt %d games but %s already holds %d. A partial "
                     "Sleeper fetch would overwrite good data with bad. Re-run, or "
                     "pass --allow-shrink if the drop is genuine."
                     % (model["gameCount"], json_path, prev.get("gameCount", 0)))
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(model, f, separators=(",", ":"))
        print("Wrote", json_path)

    build_html(model, html_path)
    print("Done. Open %s in your browser." % html_path)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>The Champions League — Record Book</title>
<style>
  :root{
    --bg:#0d1017; --panel:#151a23; --panel2:#1b2230; --line:#252d3a;
    --ink:#e7ecf3; --dim:#93a1b5; --faint:#5c6b80;
    --accent:#5B8FF9;          /* single-hue: magnitude */
    --warm:#F0A04B;            /* diverging pole: losing record */
    --cool:#5B8FF9;            /* diverging pole: winning record */
    --neutral:#2a3342;         /* diverging midpoint (neutral, never a hue) */
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--ink);padding:0 0 60px;
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  img{max-width:100%}
  header{padding:20px 22px 14px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:21px;letter-spacing:.2px}
  h2{margin:0 0 12px;font-size:15px;letter-spacing:.3px;font-weight:600}
  .sub{color:var(--dim);font-size:13px;margin-top:4px}
  .sub.stale{color:var(--warm);font-weight:600}
  .sub a{color:var(--accent);text-decoration:none}
  .sub a:hover{text-decoration:underline}
  .wrap{max-width:1180px;margin:0 auto;padding:0 16px}
  section{margin-top:30px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .tile .lbl{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.7px}
  .tile .big{font-size:27px;font-weight:700;margin:5px 0 3px;font-variant-numeric:tabular-nums}
  .tile .det{color:var(--dim);font-size:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.6px;font-weight:600;
     position:sticky;top:0;background:var(--panel);cursor:pointer;user-select:none}
  th.no{cursor:default}
  tbody tr:hover{background:#ffffff06}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
  .rank{color:var(--faint);width:34px}
  .scroll{overflow-x:auto;max-height:520px;overflow-y:auto;border-radius:8px}
  .chip{cursor:pointer;user-select:none;border:1px solid var(--line);background:var(--panel2);
    color:var(--dim);padding:4px 11px;border-radius:20px;font-size:12px}
  .chip:hover{border-color:#3a4759;color:var(--ink)}
  .chip.on{background:var(--accent);border-color:var(--accent);color:#06101f;font-weight:600}
  .row{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:12px}
  .row .lbl{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.7px}
  input.search,select{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
    border-radius:6px;padding:5px 9px;font-size:12px}
  input.search:focus,select:focus{outline:none;border-color:var(--accent)}
  .tag{font-size:10px;color:var(--faint);border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-left:6px}
  .matrix{border-collapse:separate;border-spacing:2px;font-size:11px}
  .matrix td,.matrix th{border:none;padding:5px 4px;text-align:center;border-radius:4px;white-space:nowrap}
  .matrix th{position:static;background:none}
  .matrix td.cell{font-variant-numeric:tabular-nums;cursor:default}
  .matrix td.self{background:#ffffff08;color:var(--faint)}
  .matrix th.rowh{text-align:right;color:var(--dim);font-weight:600;padding-right:8px}
  .matrix th.colh{color:var(--dim);font-weight:600;font-size:10px}
  figure{margin:0}
  figcaption{color:var(--dim);font-size:12px;margin-bottom:10px}
  .tip{position:fixed;pointer-events:none;background:#0b0f16;border:1px solid var(--line);
    border-radius:7px;padding:7px 10px;font-size:12px;color:var(--ink);z-index:50;
    box-shadow:0 6px 20px #0008;max-width:260px;opacity:0;transition:opacity .1s}
  .tip.on{opacity:1}
  .tip b{color:var(--accent)}
  .foot{color:var(--faint);font-size:11px;margin-top:8px}
  @media (max-width:560px){ .tile .big{font-size:22px} h1{font-size:18px} }
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>The Champions League <span style="color:var(--faint);font-weight:400">· Record Book</span></h1>
    <div class="sub" id="sub"></div>
    <div class="sub" id="freshness"></div>
    <div class="sub"><a href="./index.html">← Trade history timeline</a></div>
  </div>
</header>

<div class="wrap">

  <section>
    <div class="tiles" id="tiles"></div>
  </section>

  <section>
    <h2>Margin of victory — every game ever played</h2>
    <div class="card">
      <figure>
        <figcaption id="histcap"></figcaption>
        <div style="overflow-x:auto"><svg id="hist" role="img" aria-label="Distribution of margin of victory"></svg></div>
      </figure>
      <div class="foot" id="histfoot"></div>
    </div>
  </section>

  <section>
    <h2>Every game, closest first</h2>
    <div class="card">
      <div class="row">
        <span class="lbl">Season</span><span id="seasonChips" style="display:flex;gap:7px;flex-wrap:wrap"></span>
        <span class="lbl" style="margin-left:8px">Manager</span>
        <select id="mgrSel"><option value="">all</option></select>
        <input class="search" id="q" placeholder="filter…" style="margin-left:auto">
      </div>
      <div class="scroll"><table id="gamesTbl">
        <thead><tr>
          <th class="no rank">#</th><th data-k="s">Season</th><th data-k="w" class="num">Wk</th>
          <th data-k="W">Winner</th><th data-k="wp" class="num">Score</th>
          <th data-k="L">Loser</th><th data-k="lp" class="num">Score</th>
          <th data-k="m" class="num">Margin</th>
        </tr></thead><tbody></tbody>
      </table></div>
      <div class="foot" id="gamesFoot"></div>
    </div>
  </section>

  <section>
    <h2>Career record book</h2>
    <div class="card"><div class="scroll"><table id="mgrTbl">
      <thead><tr>
        <th class="no rank">#</th><th data-k="name">Manager</th><th data-k="gp" class="num">GP</th>
        <th data-k="w" class="num">W</th><th data-k="l" class="num">L</th><th data-k="pct" class="num">Win%</th>
        <th data-k="pf" class="num">PF</th><th data-k="pa" class="num">PA</th><th data-k="ppg" class="num">PPG</th>
        <th data-k="diff" class="num">Diff/G</th><th data-k="best" class="num">Best</th>
        <th data-k="worst" class="num">Worst</th><th data-k="titles" class="num">Titles</th>
      </tr></thead><tbody></tbody>
    </table></div>
    <div class="foot">Click any column to sort. PF/PA are career totals; Diff/G is average points for minus against, per game.</div>
    </div>
  </section>

  <section>
    <h2>All-time head to head</h2>
    <div class="card">
      <div style="overflow-x:auto"><table class="matrix" id="h2h"></table></div>
      <div class="foot">Row manager's record against the column manager. Blue = winning record, orange = losing, grey = even or never met. Hover a cell for detail.</div>
    </div>
  </section>

  <section>
    <h2>Title history</h2>
    <div class="card"><div class="scroll"><table id="titles">
      <thead><tr><th class="no">Season</th><th class="no">Champion</th><th class="no num">Reg. season</th><th class="no num">PPG</th></tr></thead>
      <tbody></tbody></table></div></div>
  </section>

</div>

<div class="tip" id="tip"></div>

<script>
const MODEL = /*__DATA__*/;
const $ = id => document.getElementById(id);
const f2 = n => n.toFixed(2);
const G = MODEL.games.map(g => Object.assign({}, g, {m: +(g.wp - g.lp).toFixed(2)}));
const SEASONS = MODEL.seasons.filter(s => G.some(g => g.s === s));
const CHAMPS = MODEL.champions || {};

/* ---------- tooltip ---------- */
const tip = $('tip');
function showTip(e, html){ tip.innerHTML = html; tip.classList.add('on');
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > innerHeight - 8) y = e.clientY - h - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip(){ tip.classList.remove('on'); }

/* ---------- header ---------- */
const nMgr = MODEL.managers.length;
$('sub').textContent = G.length + ' games · ' + SEASONS[0] + '–' + SEASONS[SEASONS.length-1]
  + ' · ' + nMgr + ' managers · ' + G.filter(g=>g.p).length + ' postseason';
(function(){
  const el = $('freshness'); if (!el) return;
  if (!MODEL.generated) { el.textContent = 'Refresh time unknown'; return; }
  const days = (Date.now() - MODEL.generated) / 86400000;
  const stamp = new Date(MODEL.generated).toLocaleString(undefined,
    {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  const ago = days < 1 ? Math.max(1, Math.round(days*24)) + 'h ago'
                       : Math.round(days) + ' day' + (Math.round(days)===1?'':'s') + ' ago';
  el.textContent = 'Data refreshed ' + stamp + ' (' + ago + ')';
  if (days > 8) { el.className = 'sub stale';
    el.textContent += ' — the weekly update has not run, check the Actions tab'; }
})();

/* ---------- hero tiles ---------- */
const byMargin = [...G].sort((a,b)=>a.m-b.m);
const closest = byMargin[0], widest = byMargin[byMargin.length-1];
const scores = [];
G.forEach(g => { scores.push({p:g.wp,m:g.W,s:g.s,w:g.w,won:1}); scores.push({p:g.lp,m:g.L,s:g.s,w:g.w,won:0}); });
scores.sort((a,b)=>b.p-a.p);
const hiScore = scores[0], loScore = scores[scores.length-1];
const hiLoss = [...G].sort((a,b)=>b.lp-a.lp)[0];
const sub1 = G.filter(g=>g.m<1).length;
const gm = g => g.s + ' Week ' + g.w + (g.p ? ' (postseason)' : '');
$('tiles').innerHTML = [
  ['Closest game ever', f2(closest.m), closest.W+' '+f2(closest.wp)+' def. '+closest.L+' '+f2(closest.lp)+' · '+gm(closest)],
  ['Biggest blowout', f2(widest.m), widest.W+' '+f2(widest.wp)+' def. '+widest.L+' '+f2(widest.lp)+' · '+gm(widest)],
  ['Highest score', f2(hiScore.p), hiScore.m+' · '+hiScore.s+' Week '+hiScore.w],
  ['Lowest score', f2(loScore.p), loScore.m+' · '+loScore.s+' Week '+loScore.w],
  ['Best score in a loss', f2(hiLoss.lp), hiLoss.L+' lost to '+hiLoss.W+"'s "+f2(hiLoss.wp)+' · '+gm(hiLoss)],
  ['Games under 1 point', String(sub1), 'out of ' + G.length + ' · ' + (100*sub1/G.length).toFixed(1) + '% of all games'],
].map(([l,b,d]) => '<div class="tile"><div class="lbl">'+l+'</div><div class="big">'+b+'</div><div class="det">'+esc(d)+'</div></div>').join('');

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* ---------- histogram: single series, magnitude ---------- */
(function(){
  const BW = 10;
  const maxM = Math.max(...G.map(g=>g.m));
  const nB = Math.ceil(maxM / BW);
  const bins = Array.from({length:nB}, (_,i)=>({lo:i*BW, hi:(i+1)*BW, n:0}));
  G.forEach(g => { bins[Math.min(nB-1, Math.floor(g.m/BW))].n++; });
  const W = Math.max(640, nB*54), H = 260, PL = 44, PR = 12, PT = 14, PB = 34;
  const iw = W-PL-PR, ih = H-PT-PB;
  const maxN = Math.max(...bins.map(b=>b.n));
  const yt = [0, Math.round(maxN/3), Math.round(2*maxN/3), maxN].filter((v,i,a)=>a.indexOf(v)===i);
  const y = n => PT + ih - (n/maxN)*ih;
  const bwPx = iw/nB;
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="'+W+'" height="'+H+'">';
  yt.forEach(v => { svg += '<line x1="'+PL+'" x2="'+(W-PR)+'" y1="'+y(v)+'" y2="'+y(v)+'" stroke="var(--line)" stroke-width="1"/>'
    + '<text x="'+(PL-8)+'" y="'+(y(v)+4)+'" text-anchor="end" fill="var(--faint)" font-size="10">'+v+'</text>'; });
  bins.forEach((b,i) => {
    const h = Math.max(b.n ? 2 : 0, (b.n/maxN)*ih);
    const x = PL + i*bwPx + 1;                 /* 2px surface gap between bars */
    svg += '<rect class="bar" data-i="'+i+'" x="'+x+'" y="'+(PT+ih-h)+'" width="'+(bwPx-2)+'" height="'+h
        +'" rx="4" ry="4" fill="var(--accent)"/>';
    if (i % 2 === 0 || nB <= 8)
      svg += '<text x="'+(x+(bwPx-2)/2)+'" y="'+(H-12)+'" text-anchor="middle" fill="var(--faint)" font-size="10">'+b.lo+'</text>';
  });
  svg += '<line x1="'+PL+'" x2="'+(W-PR)+'" y1="'+(PT+ih)+'" y2="'+(PT+ih)+'" stroke="var(--line)"/>';
  svg += '<text x="'+(PL-8)+'" y="'+(PT+8)+'" text-anchor="end" fill="var(--faint)" font-size="10"></text>';
  svg += '</svg>';
  $('hist').outerHTML = svg.replace('<svg ', '<svg id="hist" ');
  $('histcap').textContent = 'Games by margin of victory, in 10-point buckets. Median margin '
    + f2(byMargin[Math.floor(byMargin.length/2)].m) + ', mean ' + f2(G.reduce((a,b)=>a+b.m,0)/G.length) + '.';
  $('histfoot').textContent = G.filter(g=>g.m<5).length + ' games (' + (100*G.filter(g=>g.m<5).length/G.length).toFixed(1)
    + '%) were decided by under 5 points; ' + G.filter(g=>g.m>=50).length + ' were decided by 50 or more.';
  document.querySelectorAll('#hist .bar').forEach(r => {
    r.addEventListener('mousemove', e => { const b = bins[+r.dataset.i];
      showTip(e, '<b>'+b.lo+'–'+b.hi+' pts</b><br>'+b.n+' game'+(b.n===1?'':'s')+' ('+(100*b.n/G.length).toFixed(1)+'%)'); });
    r.addEventListener('mouseleave', hideTip);
  });
})();

/* ---------- games table ---------- */
let gState = {season:'all', mgr:'', q:'', k:'m', dir:1};
const sc = $('seasonChips');
function mkChip(val,txt){ const c=document.createElement('span'); c.className='chip'+(gState.season===val?' on':'');
  c.textContent=txt; c.onclick=()=>{ gState.season=val;
    [...sc.children].forEach(x=>x.classList.remove('on')); c.classList.add('on'); renderGames(); };
  sc.appendChild(c); }
mkChip('all','All'); SEASONS.forEach(s=>mkChip(s,s));
MODEL.managers.forEach(m => { const o=document.createElement('option'); o.value=m; o.textContent=m; $('mgrSel').appendChild(o); });
$('mgrSel').onchange = e => { gState.mgr = e.target.value; renderGames(); };
$('q').oninput = e => { gState.q = e.target.value.toLowerCase(); renderGames(); };
document.querySelectorAll('#gamesTbl th[data-k]').forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  gState.dir = gState.k === k ? -gState.dir : (k==='m'||k==='w' ? 1 : 1);
  gState.k = k; renderGames();
});
function renderGames(){
  let rows = G.filter(g =>
    (gState.season==='all' || g.s===gState.season) &&
    (!gState.mgr || g.W===gState.mgr || g.L===gState.mgr) &&
    (!gState.q || (g.W+' '+g.L+' '+g.s+' '+g.w).toLowerCase().includes(gState.q)));
  const k = gState.k;
  rows.sort((a,b) => { const x=a[k], y=b[k];
    const c = (typeof x === 'number') ? x-y : String(x).localeCompare(String(y));
    return c*gState.dir || a.m-b.m; });
  const tb = document.querySelector('#gamesTbl tbody');
  tb.innerHTML = rows.slice(0,400).map((g,i) =>
    '<tr><td class="rank">'+(i+1)+'</td><td>'+g.s+'</td><td class="num">'+g.w+(g.p?'<span class="tag">PO</span>':'')+'</td>'
    +'<td>'+esc(g.W)+'</td><td class="num">'+f2(g.wp)+'</td><td>'+esc(g.L)+'</td><td class="num">'+f2(g.lp)+'</td>'
    +'<td class="num"><b>'+f2(g.m)+'</b></td></tr>').join('');
  $('gamesFoot').textContent = rows.length + ' game' + (rows.length===1?'':'s')
    + (rows.length>400 ? ' — showing the first 400. Narrow the filters to see the rest.' : '');
}
renderGames();

/* ---------- career record book ---------- */
const stats = {};
function S(m){ return stats[m] || (stats[m] = {name:m,gp:0,w:0,l:0,pf:0,pa:0,best:0,worst:1e9,titles:0}); }
G.forEach(g => { const a=S(g.W), b=S(g.L);
  a.gp++; b.gp++; a.w++; b.l++;
  a.pf+=g.wp; a.pa+=g.lp; b.pf+=g.lp; b.pa+=g.wp;
  a.best=Math.max(a.best,g.wp); b.best=Math.max(b.best,g.lp);
  a.worst=Math.min(a.worst,g.wp); b.worst=Math.min(b.worst,g.lp); });
Object.values(CHAMPS).forEach(m => { if (stats[m]) stats[m].titles++; });
const MGR = Object.values(stats).map(s => Object.assign(s, {
  pct: s.gp ? s.w/s.gp : 0, ppg: s.gp ? s.pf/s.gp : 0, diff: s.gp ? (s.pf-s.pa)/s.gp : 0 }));
let mState = {k:'pct', dir:-1};
document.querySelectorAll('#mgrTbl th[data-k]').forEach(th => th.onclick = () => {
  const k = th.dataset.k; mState.dir = mState.k===k ? -mState.dir : -1; mState.k = k; renderMgr(); });
function renderMgr(){
  const k = mState.k;
  const rows = [...MGR].sort((a,b) => { const x=a[k],y=b[k];
    return ((typeof x==='number') ? x-y : String(x).localeCompare(String(y))) * mState.dir; });
  document.querySelector('#mgrTbl tbody').innerHTML = rows.map((s,i) =>
    '<tr><td class="rank">'+(i+1)+'</td><td>'+esc(s.name)+(s.titles?'<span class="tag">'+s.titles+'× champ</span>':'')+'</td>'
    +'<td class="num">'+s.gp+'</td><td class="num">'+s.w+'</td><td class="num">'+s.l+'</td>'
    +'<td class="num">'+(100*s.pct).toFixed(1)+'%</td><td class="num">'+s.pf.toFixed(1)+'</td>'
    +'<td class="num">'+s.pa.toFixed(1)+'</td><td class="num">'+s.ppg.toFixed(2)+'</td>'
    +'<td class="num">'+(s.diff>=0?'+':'')+s.diff.toFixed(2)+'</td>'
    +'<td class="num">'+f2(s.best)+'</td><td class="num">'+f2(s.worst)+'</td>'
    +'<td class="num">'+(s.titles||'')+'</td></tr>').join('');
}
renderMgr();

/* ---------- head to head (diverging: cool / neutral / warm) ---------- */
(function(){
  const M = MGR.map(s=>s.name).sort();
  const h = {};
  M.forEach(a => { h[a]={}; M.forEach(b => h[a][b]={w:0,l:0,pf:0,pa:0}); });
  G.forEach(g => { if(!h[g.W]||!h[g.W][g.L]) return;
    h[g.W][g.L].w++; h[g.W][g.L].pf+=g.wp; h[g.W][g.L].pa+=g.lp;
    h[g.L][g.W].l++; h[g.L][g.W].pf+=g.lp; h[g.L][g.W].pa+=g.wp; });
  const short = n => n.length>9 ? n.slice(0,8)+'…' : n;
  let html = '<tr><th class="rowh"></th>' + M.map(m=>'<th class="colh" title="'+esc(m)+'">'+esc(short(m))+'</th>').join('') + '</tr>';
  M.forEach(a => {
    html += '<tr><th class="rowh" title="'+esc(a)+'">'+esc(short(a))+'</th>';
    M.forEach(b => {
      if (a===b) { html += '<td class="self">—</td>'; return; }
      const c = h[a][b], n = c.w+c.l;
      if (!n) { html += '<td class="cell" style="background:var(--neutral);color:var(--faint)">·</td>'; return; }
      const r = c.w/n, t = Math.min(1, Math.abs(r-0.5)*2);
      const bg = r>0.5 ? 'rgba(91,143,249,'+(0.12+0.5*t).toFixed(2)+')'
               : r<0.5 ? 'rgba(240,160,75,'+(0.12+0.5*t).toFixed(2)+')' : 'var(--neutral)';
      html += '<td class="cell" style="background:'+bg+'" data-a="'+esc(a)+'" data-b="'+esc(b)+'">'+c.w+'–'+c.l+'</td>';
    });
    html += '</tr>';
  });
  $('h2h').innerHTML = html;
  $('h2h').querySelectorAll('td.cell[data-a]').forEach(td => {
    td.addEventListener('mousemove', e => { const a=td.dataset.a, b=td.dataset.b, c=h[a][b], n=c.w+c.l;
      showTip(e, '<b>'+esc(a)+' vs '+esc(b)+'</b><br>'+c.w+'–'+c.l+' ('+(100*c.w/n).toFixed(0)+'%)<br>'
        +c.pf.toFixed(1)+' for · '+c.pa.toFixed(1)+' against<br>'+((c.pf-c.pa)/n>=0?'+':'')+((c.pf-c.pa)/n).toFixed(2)+' per game'); });
    td.addEventListener('mouseleave', hideTip);
  });
})();

/* ---------- titles ---------- */
(function(){
  const rows = SEASONS.map(s => {
    const champ = CHAMPS[s];
    if (!champ) return '<tr><td>'+s+'</td><td style="color:var(--faint)">in progress</td><td class="num">—</td><td class="num">—</td></tr>';
    const reg = G.filter(g => g.s===s && !g.p && (g.W===champ||g.L===champ));
    const w = reg.filter(g=>g.W===champ).length, l = reg.length-w;
    const pts = reg.reduce((a,g)=>a+(g.W===champ?g.wp:g.lp),0);
    return '<tr><td>'+s+'</td><td><b>'+esc(champ)+'</b></td><td class="num">'+w+'–'+l+'</td><td class="num">'
      +(reg.length?(pts/reg.length).toFixed(2):'—')+'</td></tr>';
  }).join('');
  document.querySelector('#titles tbody').innerHTML = rows;
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
