"""A static status page for the pipeline.

Renders from the season database itself — run history, coverage, quality — so
the page cannot claim anything the data does not support. Self-contained HTML
with no external requests, because it is served from GitHub Pages.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bsetl.quality import run_quality_checks
from bsetl.transform.seasons import (
    current_season,
    days_until_next_season,
    season_bounds,
    season_for_database,
)

_SEV_CLASS = {"ok": "ok", "warn": "warn", "fail": "fail", "skip": "skip"}


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def collect(db_path: str) -> dict:
    """Everything the page shows, gathered in one pass."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = (_rows(conn, "SELECT COUNT(*) FROM matches") or [(0,)])[0][0]
        span = (_rows(conn, "SELECT MIN(battle_time), MAX(battle_time) FROM matches")
                or [(None, None)])[0]
        daily = _rows(conn, "SELECT substr(battle_time,1,8) d, COUNT(*) n FROM matches "
                            "WHERE battle_time IS NOT NULL GROUP BY d ORDER BY d")
        modes = _rows(conn, "SELECT mode, COUNT(*) n FROM matches WHERE mode IS NOT NULL "
                            "GROUP BY mode ORDER BY n DESC")
        runs = _rows(conn, "SELECT started_utc, status, stop_reason, requests_made, "
                           "rows_inserted, elapsed_seconds, frontier_after "
                           "FROM pipeline_runs ORDER BY started_utc DESC LIMIT 15")
        frontier = (_rows(conn, "SELECT COUNT(*) FROM crawl_frontier") or [(0,)])[0][0]
        skill = _rows(conn, "SELECT COUNT(*), SUM(COALESCE(skill_ns_ok,0)) FROM matches")
    finally:
        conn.close()

    report = run_quality_checks(db_path)
    season = season_for_database(db_path) or "unknown"
    coverage = None
    if skill and skill[0][0] and skill[0][1] is not None:
        coverage = skill[0][1] / skill[0][0]

    return {
        "season": season, "total": total, "span": span, "daily": daily,
        "modes": modes, "runs": runs, "frontier": frontier, "coverage": coverage,
        "report": report, "generated": datetime.now(tz=UTC),
    }


def _fmt_day(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def render_dashboard(db_path: str) -> str:
    d = collect(db_path)
    e = html.escape
    total, daily, report = d["total"], d["daily"], d["report"]
    peak = max((n for _, n in daily), default=1) or 1

    bars = "".join(
        f'<div class="bar" style="height:{max(2, round(n / peak * 100))}%" '
        f'title="{_fmt_day(day)}: {n:,} sets"></div>'
        for day, n in daily
    ) or '<p class="muted">No matches yet.</p>'

    checks = "".join(
        f'<tr><td><span class="pill {_SEV_CLASS.get(r.severity.value, "skip")}">'
        f"{r.severity.value}</span></td><td><code>{e(r.name)}</code></td>"
        f"<td>{e(r.message)}</td></tr>"
        for r in report.results
    )

    runs = "".join(
        f"<tr><td>{e(str(s or '')[:16].replace('T', ' '))}</td>"
        f'<td><span class="pill {"ok" if st == "ok" else "fail"}">{e(str(st))}</span></td>'
        f"<td><code>{e(str(sr or '—'))}</code></td><td>{(rq or 0):,}</td>"
        f"<td>{(ri or 0):,}</td><td>{(el or 0):.0f}s</td><td>{(fa or 0):,}</td></tr>"
        for s, st, sr, rq, ri, el, fa in d["runs"]
    ) or '<tr><td colspan="7" class="muted">No runs recorded yet.</td></tr>'

    modes = "".join(
        f'<tr><td><code>{e(m)}</code></td><td>{n:,}</td>'
        f'<td>{n / total:.1%}</td></tr>' for m, n in d["modes"]
    ) if total else ""

    lo, hi = d["span"]
    cov = "—" if d["coverage"] is None else f"{d['coverage']:.1%}"
    status_cls = "ok" if report.ok else "fail"
    status_txt = "passing" if report.ok else "failing"
    _, season_end = season_bounds(int(current_season().removeprefix("season")))

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brawl Stars ETL — pipeline status</title>
<style>
:root {{
  --bg:#fbfbfd; --fg:#16181d; --muted:#6a7180; --card:#fff; --line:#e4e7ec;
  --ok:#0f7b3f; --okbg:#e6f5ec; --warn:#8a5a00; --warnbg:#fdf3e0;
  --fail:#b3261e; --failbg:#fdecea; --accent:#2d6cdf;
}}
@media (prefers-color-scheme:dark) {{ :root {{
  --bg:#0f1115; --fg:#e6e8ec; --muted:#98a0ae; --card:#171a21; --line:#262b35;
  --ok:#5fd39a; --okbg:#12291f; --warn:#e0b463; --warnbg:#2a2213;
  --fail:#f2837c; --failbg:#2d1917; --accent:#7aa5f0;
}} }}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:940px;margin:0 auto;padding:2.5rem 1.25rem 4rem}}
h1{{font-size:1.5rem;margin:0 0 .25rem}}
h2{{font-size:1rem;letter-spacing:.02em;text-transform:uppercase;color:var(--muted);
 margin:2.25rem 0 .75rem;font-weight:600}}
.muted{{color:var(--muted)}}
.sub{{color:var(--muted);margin:0 0 1.5rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem}}
.card .k{{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
.card .v{{font-size:1.5rem;font-weight:650;margin-top:.2rem}}
.chart{{display:flex;align-items:flex-end;gap:2px;height:120px;background:var(--card);
 border:1px solid var(--line);border-radius:10px;padding:.75rem}}
.bar{{flex:1;min-width:2px;background:var(--accent);border-radius:2px 2px 0 0;opacity:.85}}
.bar:hover{{opacity:1}}
table{{width:100%;border-collapse:collapse;background:var(--card);
 border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--line);font-size:.88rem}}
th{{color:var(--muted);font-weight:600;font-size:.75rem;text-transform:uppercase}}
tr:last-child td{{border-bottom:0}}
code{{font:.85em ui-monospace,SFMono-Regular,Menlo,monospace}}
.pill{{display:inline-block;padding:.1rem .5rem;border-radius:99px;font-size:.72rem;
 font-weight:650;text-transform:uppercase;letter-spacing:.03em}}
.pill.ok{{background:var(--okbg);color:var(--ok)}}
.pill.warn{{background:var(--warnbg);color:var(--warn)}}
.pill.fail{{background:var(--failbg);color:var(--fail)}}
.pill.skip{{background:var(--line);color:var(--muted)}}
.overflow{{overflow-x:auto}}
footer{{margin-top:3rem;color:var(--muted);font-size:.82rem}}
a{{color:var(--accent)}}
</style>
<div class="wrap">
<h1>Brawl Stars ranked telemetry — pipeline status</h1>
<p class="sub">Generated {d['generated']:%Y-%m-%d %H:%M} UTC ·
 current season <strong>{e(current_season())}</strong>, resets {season_end}
 ({days_until_next_season()} days)</p>

<div class="grid">
  <div class="card"><div class="k">Ranked sets</div><div class="v">{total:,}</div></div>
  <div class="card"><div class="k">Season in database</div><div class="v">{e(d['season'])}</div></div>
  <div class="card"><div class="k">Quality gate</div>
    <div class="v"><span class="pill {status_cls}">{status_txt}</span></div></div>
  <div class="card"><div class="k">skill_ns coverage</div><div class="v">{cov}</div></div>
  <div class="card"><div class="k">Frontier pending</div><div class="v">{d['frontier']:,}</div></div>
</div>

<h2>Sets per day</h2>
<div class="chart">{bars}</div>
<p class="muted" style="font-size:.82rem">
 {_fmt_day(lo or '')} to {_fmt_day(hi or '')} · {len(daily)} days</p>

<h2>Quality checks</h2>
<div class="overflow"><table>
<tr><th>Result</th><th>Check</th><th>Detail</th></tr>{checks}</table></div>

<h2>Recent runs</h2>
<div class="overflow"><table>
<tr><th>Started</th><th>Status</th><th>Stopped because</th><th>Requests</th>
<th>Rows added</th><th>Elapsed</th><th>Frontier left</th></tr>{runs}</table></div>

{"<h2>Modes</h2><div class='overflow'><table><tr><th>Mode</th><th>Sets</th><th>Share</th></tr>" + modes + "</table></div>" if modes else ""}

<footer>Built by an automated ETL pipeline ·
 <a href="https://github.com/elixf7/brawlstars-data-pipeline">source</a></footer>
</div>
"""


def write_dashboard(db_path: str, out_path: str) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(db_path), encoding="utf-8")
    return out


def write_status_json(db_path: str, out_path: str) -> Path:
    """Machine-readable twin of the page, for badges or external monitoring."""
    d = collect(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_utc": d["generated"].isoformat(),
        "season_in_database": d["season"],
        "current_season": current_season(),
        "total_sets": d["total"],
        "first_match": d["span"][0],
        "last_match": d["span"][1],
        "days_covered": len(d["daily"]),
        "frontier_pending": d["frontier"],
        "skill_ns_coverage": d["coverage"],
        "quality_ok": d["report"].ok,
        "quality": d["report"].to_dict()["counts"],
    }, indent=2))
    return out
