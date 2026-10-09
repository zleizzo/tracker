"""End-of-day reflections. The database is the source of truth; every save also writes
<data_dir>/reflections/<day>.md with the day's statistics in a YAML front matter, plus an index.md,
so another tool (or another Claude) can read the whole journal from plain files."""
import json
import os
import re
import time
from datetime import datetime

from . import analytics, db
from .analytics import Filters, day_bounds

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def export_dir():
    return os.path.join(db.data_dir(), "reflections")


def _check_day(day):
    if not day or not _DAY_RE.match(day):
        raise ValueError("day must be YYYY-MM-DD")
    analytics.parse_day(day)
    return day


def get(conn, day):
    r = conn.execute("SELECT * FROM reflections WHERE day = ?", (_check_day(day),)).fetchone()
    return dict(r) if r else None


def list_entries(conn, since=None, limit=None):
    sql = "SELECT * FROM reflections"
    params = []
    if since:
        sql += " WHERE day >= ?"
        params.append(_check_day(since))
    sql += " ORDER BY day DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def save(conn, day, text, rating=None):
    day = _check_day(day)
    text = (text or "").strip()
    if rating in ("", None):
        rating = None
    else:
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError("rating must be between 1 and 5")
    now = time.time()
    existing = get(conn, day)
    if existing:
        conn.execute("UPDATE reflections SET text = ?, rating = ?, updated_at = ? WHERE day = ?", (text, rating, now, day))
    else:
        conn.execute("INSERT INTO reflections(day, text, rating, created_at, updated_at) VALUES (?,?,?,?,?)", (day, text, rating, now, now))
    conn.commit()
    write_markdown(conn, day)
    write_index(conn)
    return get(conn, day)


def delete(conn, day):
    day = _check_day(day)
    cur = conn.execute("DELETE FROM reflections WHERE day = ?", (day,))
    conn.commit()
    path = os.path.join(export_dir(), f"{day}.md")
    if os.path.exists(path):
        os.remove(path)
    write_index(conn)
    return cur.rowcount > 0


# -- statistics that travel with the entry --------------------------------------

def day_stats(conn, day):
    t0, t1 = day_bounds(day)
    s = analytics.summary(conn, t0, t1, Filters())
    t = s["totals"]
    review = analytics.plan_review(conn, day)
    pt = review["totals"]
    focus = [dict(r) for r in conn.execute("SELECT * FROM focus_sessions WHERE start < ? AND end > ? ORDER BY start", (t1, t0))]
    focus_minutes = sum(max(0.0, min(f["stopped_at"] or f["end"], t1) - max(f["start"], t0)) for f in focus) / 60
    h = lambda secs: round((secs or 0) / 3600, 2)
    return {
        "weekday": analytics.parse_day(day).strftime("%A"),
        "tracked_hours": h(t["active"]),
        "productive_hours": h(t["productive"]),
        "neutral_hours": h(t["neutral"]),
        "distracting_hours": h(t["distracting"]),
        "context_switching_hours": h(t["switching"]),
        "productive_pct": t["pulse"],
        "switches": t["switches"]["total"],
        "breaks_hours": h(t["idle"]),
        "logged_manually_hours": h(t["manual"]),
        "projects": [f"{p['title']}: {h(p['seconds'])}h" for p in s["by_project"][:6]],
        "top_apps": [f"{a['app']}: {h(a['seconds'])}h" for a in s["by_app"][:5]],
        "top_sites": [f"{d['domain']}: {h(d['seconds'])}h" for d in s["by_domain"][:5]],
        "plan_blocks": len(review["blocks"]),
        "plan_adherence_pct": None if pt["adherence"] is None else round(100 * pt["adherence"]),
        "plan_outside_hours": h(pt["outside"]) if review["blocks"] else None,
        "focus_sessions": len(focus),
        "focus_minutes": round(focus_minutes),
    }


def _yaml_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_yaml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def to_markdown(entry, stats):
    fm = {"day": entry["day"], "rating": entry.get("rating"),
          "written_at": datetime.fromtimestamp(entry["created_at"]).isoformat(timespec="minutes") if entry.get("created_at") else None,
          "updated_at": datetime.fromtimestamp(entry["updated_at"]).isoformat(timespec="minutes") if entry.get("updated_at") else None}
    fm.update(stats)
    lines = ["---"] + [f"{k}: {_yaml_value(v)}" for k, v in fm.items()] + ["---", "",
             f"# {entry['day']} ({stats.get('weekday', '')})", "", entry.get("text") or "_(no text)_", ""]
    return "\n".join(lines)


def write_markdown(conn, day):
    entry = get(conn, day)
    if not entry:
        return None
    os.makedirs(export_dir(), exist_ok=True)
    path = os.path.join(export_dir(), f"{day}.md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(entry, day_stats(conn, day)))
    os.replace(tmp, path)
    return path


def write_index(conn):
    os.makedirs(export_dir(), exist_ok=True)
    rows = list_entries(conn)
    lines = ["# Daily reflections", "",
             "One file per day, newest first. Each file starts with a YAML front matter holding that day's",
             "tracked statistics (hours by category, projects, plan adherence, focus sessions) followed by the",
             "reflection text. Regenerate with `bin/tracker reflections --export`; print them all with",
             "`bin/tracker reflections` (or `--since YYYY-MM-DD`, `--json`).", "",
             "| Day | Rating | First line |", "|---|---|---|"]
    for r in rows:
        first = (r["text"] or "").strip().split("\n")[0][:90]
        lines.append(f"| [{r['day']}]({r['day']}.md) | {r['rating'] if r['rating'] else ''} | {first.replace('|', '/')} |")
    with open(os.path.join(export_dir(), "index.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def export_all(conn):
    n = 0
    for r in list_entries(conn):
        write_markdown(conn, r["day"])
        n += 1
    write_index(conn)
    return n


def render_all(conn, since=None):
    """All reflections as one Markdown document (what `tracker reflections` prints)."""
    out = []
    for r in list_entries(conn, since):
        out.append(to_markdown(r, day_stats(conn, r["day"])))
    return "\n\n".join(out)
