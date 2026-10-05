"""Aggregations behind the dashboard: totals, breakdowns, trends, timeline, sessions, export."""
import csv
import io
import time
from datetime import datetime, timedelta

from .classify import (BROWSERS, CATEGORIES, SWITCHING, Classifier, apply_manual_entries,
                       build_sessions, split_hourly)
from .db import get_settings, rows

CAT_KEYS = ("productive", "neutral", "distracting", "switching")
SWITCH_KINDS = ("app", "window", "tab")


class Filters:
    """Everything the filter row can narrow the data by."""

    def __init__(self, project=None, category=None, hours=(0, 24), weekdays=None, bundle=None):
        self.project = project          # None | 'unassigned' | 'cs' | int project id
        self.category = category        # None | productive | neutral | distracting | switching
        self.hours = hours              # [h0, h1) local hours
        self.weekdays = set(weekdays) if weekdays is not None else set(range(7))
        self.bundle = bundle

    @classmethod
    def from_query(cls, q):
        project = q.get("project") or None
        if project and project not in ("unassigned", "cs"):
            try:
                project = int(project)
            except ValueError:
                project = None
        hours = (0, 24)
        if q.get("hours"):
            try:
                a, b = q["hours"].split("-")
                hours = (max(0, int(a)), min(24, int(b)))
            except ValueError:
                pass
        weekdays = None
        if q.get("weekdays"):
            try:
                weekdays = {int(x) for x in q["weekdays"].split(",") if x != ""}
            except ValueError:
                weekdays = None
        return cls(project=project, category=q.get("category") or None, hours=hours, weekdays=weekdays,
                   bundle=q.get("bundle") or None)

    def passes_time(self, hour, weekday):
        return self.hours[0] <= hour < self.hours[1] and weekday in self.weekdays

    def passes_segment(self, seg):
        if self.bundle and seg.get("bundle") != self.bundle:
            return False
        if self.category and (seg.get("category") or "neutral") != self.category:
            return False
        if self.project == "cs":
            return bool(seg.get("cs"))
        if self.project == "unassigned":
            return not seg.get("cs") and seg.get("project_id") is None
        if isinstance(self.project, int):
            return seg.get("project_id") == self.project
        return True


# -- time helpers --------------------------------------------------------------

def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d")


def day_bounds(day):
    d = parse_day(day)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def range_bounds(from_day, to_day):
    return parse_day(from_day).timestamp(), (parse_day(to_day) + timedelta(days=1)).timestamp()


def days_between(t0, t1):
    d = datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(t1 - 1)
    out = []
    while d <= end:
        out.append((d.strftime("%Y-%m-%d"), d.weekday()))
        d += timedelta(days=1)
    return out


def parse_time(v):
    """Accept unix seconds or a local ISO string like 2026-10-03T14:30."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    return datetime.fromisoformat(s).timestamp()


# -- loading -------------------------------------------------------------------

def load_segments(conn, t0, t1):
    segs = rows(conn, "SELECT * FROM segments WHERE end > ? AND start < ? ORDER BY start", (t0, t1))
    for s in segs:
        s["start"] = max(s["start"], t0)
        s["end"] = min(s["end"], t1)
    return [s for s in segs if s["end"] > s["start"]]


def load_manual(conn, t0, t1):
    entries = rows(conn, "SELECT * FROM manual_entries WHERE end > ? AND start < ? ORDER BY start", (t0, t1))
    for e in entries:
        e["start"] = max(e["start"], t0)
        e["end"] = min(e["end"], t1)
    return [e for e in entries if e["end"] > e["start"]]


def labelled_segments(conn, t0, t1, settings=None):
    settings = settings or get_settings(conn)
    segs = apply_manual_entries(load_segments(conn, t0, t1), load_manual(conn, t0, t1))
    clf = Classifier(conn, settings)
    clf.classify(segs)
    return segs, clf


def project_info(clf, key):
    if key == "cs":
        return {"id": "cs", "title": "Context switching", "color": None, "category": SWITCHING}
    if key == "none":
        return {"id": None, "title": "Unassigned", "color": None, "category": "neutral"}
    p = clf.projects.get(key)
    if not p:
        return {"id": key, "title": f"Project {key}", "color": None, "category": "neutral"}
    return {"id": p["id"], "title": p["title"], "color": p["color"], "category": p["category"]}


def _bump(d, key, dur, **extra):
    e = d.get(key)
    if e is None:
        e = d[key] = {"seconds": 0.0, **extra}
    e["seconds"] += dur
    return e


def _sorted(d, limit=None):
    out = sorted(d.values(), key=lambda e: -e["seconds"])
    return out[:limit] if limit else out


# -- the big one ---------------------------------------------------------------

def summary(conn, t0, t1, f: Filters):
    settings = get_settings(conn)
    segs, clf = labelled_segments(conn, t0, t1, settings)

    totals = {k: 0.0 for k in CAT_KEYS}
    idle = 0.0
    manual = 0.0
    switches = {k: 0 for k in SWITCH_KINDS}
    by_project, by_task, by_app, by_domain, by_workspace, by_activity = {}, {}, {}, {}, {}, {}
    days = {}
    for day, wd in days_between(t0, t1):
        days[day] = {"day": day, "weekday": wd, "idle": 0.0, "switches": 0, "by_project": {}, **{k: 0.0 for k in CAT_KEYS}}
    per_hour = [{"hour": h, "switches": 0, **{k: 0.0 for k in CAT_KEYS}} for h in range(24)]
    per_weekday = [{"weekday": w, "switches": 0, **{k: 0.0 for k in CAT_KEYS}} for w in range(7)]
    heat = [[0.0] * 24 for _ in range(7)]
    idle_chunks = []
    day_span = {}

    for s in segs:
        kind = s["kind"]
        counted_switch = False
        for idx, (a, b, h, wd, day) in enumerate(split_hourly(s["start"], s["end"])):
            if not f.passes_time(h, wd):
                continue
            dur = b - a
            d = days.get(day)
            if d is None:
                d = days[day] = {"day": day, "weekday": wd, "idle": 0.0, "switches": 0, "by_project": {}, **{k: 0.0 for k in CAT_KEYS}}
            if kind in ("idle", "locked"):
                if not f.project and not f.category and not f.bundle:
                    idle_chunks.append((day, a, b))
                continue
            if not f.passes_segment(s):
                continue
            span = day_span.get(day)
            day_span[day] = (a, b) if span is None else (min(span[0], a), max(span[1], b))
            cat = s.get("category") or "neutral"
            totals[cat] += dur
            d[cat] += dur
            per_hour[h][cat] += dur
            per_weekday[wd][cat] += dur
            heat[wd][h] += dur
            if kind == "manual":
                manual += dur
            pkey = "cs" if s.get("cs") else (s.get("project_id") or "none")
            d["by_project"][str(pkey)] = d["by_project"].get(str(pkey), 0.0) + dur
            _bump(by_project, pkey, dur, **project_info(clf, pkey))
            if s.get("task_id"):
                t = clf.tasks.get(s["task_id"])
                if t:
                    _bump(by_task, t["id"], dur, id=t["id"], title=t["title"], project_id=t["project_id"],
                          project=clf.projects.get(t["project_id"], {}).get("title", ""), done=bool(t["done"]))
            appkey = "manual" if kind == "manual" else (s.get("bundle") or s.get("app") or "?")
            appname = "Logged manually" if kind == "manual" else (s.get("app") or appkey)
            ae = _bump(by_app, appkey, dur, bundle=appkey, app=appname, categories={})
            ae["categories"][cat] = ae["categories"].get(cat, 0.0) + dur
            if s.get("domain"):
                de = _bump(by_domain, s["domain"], dur, domain=s["domain"], categories={}, projects={})
                de["categories"][cat] = de["categories"].get(cat, 0.0) + dur
                de["projects"][str(pkey)] = de["projects"].get(str(pkey), 0.0) + dur
            if s.get("workspace"):
                _bump(by_workspace, (appkey, s["workspace"]), dur, app=s.get("app"), workspace=s["workspace"])
            akey = (appname, s.get("title_clean") or s.get("context") or "")
            act = _bump(by_activity, akey, dur, app=appname, title=akey[1], context=s.get("context") or "",
                        projects={}, categories={})
            act["projects"][str(pkey)] = act["projects"].get(str(pkey), 0.0) + dur
            act["categories"][cat] = act["categories"].get(cat, 0.0) + dur
            if idx == 0 and not counted_switch and kind == "active" and s.get("switch_kind") in switches:
                switches[s["switch_kind"]] += 1
                d["switches"] += 1
                per_hour[h]["switches"] += 1
                per_weekday[wd]["switches"] += 1
                counted_switch = True

    # Idle only counts between the first and last activity of a day: breaks, not nights.
    for day, a, b in idle_chunks:
        span = day_span.get(day)
        if not span:
            continue
        overlap = min(b, span[1]) - max(a, span[0])
        if overlap > 0:
            idle += overlap
            days[day]["idle"] += overlap

    for act in by_activity.values():
        act["project"] = max(act["projects"], key=act["projects"].get) if act["projects"] else "none"
        act["category"] = max(act["categories"], key=act["categories"].get) if act["categories"] else "neutral"
        del act["projects"]
    for de in by_domain.values():
        de["project"] = max(de["projects"], key=de["projects"].get) if de["projects"] else "none"
        de["category"] = max(de["categories"], key=de["categories"].get) if de["categories"] else "neutral"
        del de["projects"]
    for ae in by_app.values():
        ae["category"] = max(ae["categories"], key=ae["categories"].get) if ae["categories"] else "neutral"

    active = sum(totals.values())
    attributable = totals["productive"] + totals["neutral"] + totals["distracting"]
    pulse = round(100.0 * totals["productive"] / attributable) if attributable > 0 else None
    sessions = build_sessions(segs, float(settings.get("session_gap_seconds", 120)))
    longest = sorted(sessions, key=lambda s: -s["seconds"])[:8]

    return {
        "range": {"from": t0, "to": t1, "days": len(days)},
        "totals": {**totals, "active": active, "idle": idle, "manual": manual, "pulse": pulse,
                   "switches": {**switches, "total": sum(switches.values())}},
        "by_project": _sorted(by_project),
        "by_task": _sorted(by_task),
        "by_app": _sorted(by_app, 30),
        "by_domain": _sorted(by_domain, 30),
        "by_workspace": _sorted(by_workspace, 30),
        "top_activities": _sorted(by_activity, 60),
        "per_day": [days[k] for k in sorted(days)],
        "per_hour": per_hour,
        "per_weekday": per_weekday,
        "heatmap": heat,
        "longest_sessions": longest,
        "plan": plan_totals_for_range(conn, t0, t1, segs),
        "projects": sorted(clf.projects.values(), key=lambda p: (p["archived"], p["title"].lower())),
    }


# -- timeline / sessions -------------------------------------------------------

SEG_FIELDS = ("id", "start", "end", "kind", "app", "bundle", "url", "domain", "workspace",
              "project_id", "task_id", "category", "cs", "source", "switch_kind", "manual_id", "note")


def timeline(conn, day):
    settings = get_settings(conn)
    t0, t1 = day_bounds(day)
    segs, clf = labelled_segments(conn, t0, t1, settings)
    out_segs = []
    for s in segs:
        o = {k: s.get(k) for k in SEG_FIELDS if s.get(k) is not None}
        o["title"] = s.get("title_clean") or s.get("title") or ""
        out_segs.append(o)
    sessions = build_sessions(segs, float(settings.get("session_gap_seconds", 120)))
    min_gap = float(settings.get("min_idle_gap_minutes", 5)) * 60
    gaps = [{"start": s["start"], "end": s["end"], "kind": s["kind"]}
            for s in segs if s["kind"] in ("idle", "locked") and s["end"] - s["start"] >= min_gap]
    return {
        "day": day, "t0": t0, "t1": t1,
        "segments": out_segs,
        "sessions": sessions,
        "gaps": gaps,
        "assignments": rows(conn, "SELECT * FROM assignments WHERE end > ? AND start < ? ORDER BY start", (t0, t1)),
        "manual": rows(conn, "SELECT * FROM manual_entries WHERE end > ? AND start < ? ORDER BY start", (t0, t1)),
        "plan_blocks": plan_blocks(conn, day),
        "projects": sorted(clf.projects.values(), key=lambda p: (p["archived"], p["title"].lower())),
        "tasks": list(clf.tasks.values()),
    }


def unsorted(conn, t0, t1, limit=40):
    """Apps / sites / workspaces with time but no rule yet, as candidate rules."""
    segs, clf = labelled_segments(conn, t0, t1)
    agg = {}
    for s in segs:
        if s.get("kind") != "active" or s.get("cs") or s.get("source") != "default":
            continue
        dur = s["end"] - s["start"]
        if s.get("bundle") in BROWSERS and s.get("domain"):
            key = ("domain", s["domain"])
        elif s.get("workspace"):
            key = ("workspace", s["workspace"])
        else:
            key = ("app", s.get("app") or s.get("bundle") or "?")
        e = agg.get(key)
        if e is None:
            e = agg[key] = {"field": key[0], "pattern": key[1], "app": s.get("app"), "bundle": s.get("bundle"),
                            "seconds": 0.0, "titles": {}}
        e["seconds"] += dur
        tc = s.get("title_clean") or ""
        if tc:
            e["titles"][tc] = e["titles"].get(tc, 0.0) + dur
    out = sorted(agg.values(), key=lambda e: -e["seconds"])[:limit]
    for e in out:
        e["titles"] = [t for t, _ in sorted(e["titles"].items(), key=lambda kv: -kv[1])[:4]]
    return out


def export_csv(conn, t0, t1):
    segs, clf = labelled_segments(conn, t0, t1)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["start", "end", "seconds", "kind", "app", "bundle", "title", "url", "domain", "workspace",
                "project", "task", "category", "source", "context_switching", "switch_kind"])
    for s in segs:
        p = clf.projects.get(s.get("project_id"), {}).get("title", "") if s.get("project_id") else ""
        t = clf.tasks.get(s.get("task_id"), {}).get("title", "") if s.get("task_id") else ""
        w.writerow([
            datetime.fromtimestamp(s["start"]).isoformat(timespec="seconds"),
            datetime.fromtimestamp(s["end"]).isoformat(timespec="seconds"),
            round(s["end"] - s["start"], 1), s["kind"], s.get("app") or "", s.get("bundle") or "",
            s.get("title_clean") or s.get("title") or "", s.get("url") or "", s.get("domain") or "",
            s.get("workspace") or "", p, t, s.get("category") or "", s.get("source") or "",
            int(bool(s.get("cs"))), s.get("switch_kind") or "",
        ])
    return buf.getvalue()


# -- daily plan: blocks vs what actually happened ------------------------------

WORK_KINDS = ("active", "manual")


def is_work(seg):
    return seg.get("kind") in WORK_KINDS and not seg.get("cs")


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def plan_blocks(conn, day):
    return rows(conn, """
        SELECT b.*, p.title AS project_title, p.color AS project_color, t.title AS task_title
        FROM plan_blocks b LEFT JOIN projects p ON p.id = b.project_id LEFT JOIN tasks t ON t.id = b.task_id
        WHERE b.day = ? ORDER BY b.start""", (day,))


def _top_actual(actual, clf, limit=4):
    out = []
    for key, secs in sorted(actual.items(), key=lambda kv: -kv[1])[:limit]:
        info = project_info(clf, "none" if key == "none" else int(key))
        out.append({"key": key, "title": info["title"], "color": info["color"], "seconds": secs})
    return out


def plan_review(conn, day, segs=None, clf=None, now=None):
    """How the day's plan compared with reality. For today the numbers are 'so far'."""
    now = now or time.time()
    t0, t1 = day_bounds(day)
    blocks = plan_blocks(conn, day)
    if segs is None:
        segs, clf = labelled_segments(conn, t0, t1)
    work = [s for s in segs if is_work(s)]
    idle = [s for s in segs if s["kind"] in ("idle", "locked")]
    tot = {"planned": 0.0, "elapsed": 0.0, "on_plan": 0.0, "on_task": 0.0, "off_plan": 0.0, "idle_in_blocks": 0.0}
    planned_by, on_plan_by = {}, {}
    for b in blocks:
        dur = b["end"] - b["start"]
        elapsed = max(0.0, min(b["end"], now) - b["start"])
        actual, on_plan, on_task, work_in, idle_in = {}, 0.0, 0.0, 0.0, 0.0
        for s in work:
            o = _overlap(s["start"], s["end"], b["start"], b["end"])
            if o <= 0:
                continue
            key = str(s.get("project_id") or "none")
            actual[key] = actual.get(key, 0.0) + o
            work_in += o
            if b["project_id"] and s.get("project_id") == b["project_id"]:
                on_plan += o
                if b["task_id"] and s.get("task_id") == b["task_id"]:
                    on_task += o
        for s in idle:
            o = _overlap(s["start"], s["end"], b["start"], b["end"])
            if o > 0:
                idle_in += o
        b.update(seconds=dur, elapsed=elapsed, on_plan=on_plan, on_task=on_task, work=work_in, idle=idle_in,
                 actual=_top_actual(actual, clf), adherence=(on_plan / elapsed if elapsed > 0 else None))
        tot["planned"] += dur
        tot["elapsed"] += elapsed
        tot["on_plan"] += on_plan
        tot["on_task"] += on_task
        tot["off_plan"] += max(0.0, work_in - on_plan)
        tot["idle_in_blocks"] += idle_in
        pk = str(b["project_id"] or "none")
        planned_by[pk] = planned_by.get(pk, 0.0) + dur
        on_plan_by[pk] = on_plan_by.get(pk, 0.0) + on_plan
    actual_by, outside, total_work = {}, 0.0, 0.0
    for s in work:
        d = s["end"] - s["start"]
        total_work += d
        key = str(s.get("project_id") or "none")
        actual_by[key] = actual_by.get(key, 0.0) + d
        covered = sum(max(0.0, _overlap(s["start"], s["end"], b["start"], b["end"])) for b in blocks)
        outside += max(0.0, d - min(d, covered))
    projects = []
    for key in set(planned_by) | set(actual_by):
        info = project_info(clf, "none" if key == "none" else int(key))
        projects.append({"key": key, "id": info["id"], "title": info["title"], "color": info["color"],
                         "planned": planned_by.get(key, 0.0), "actual": actual_by.get(key, 0.0),
                         "on_plan": on_plan_by.get(key, 0.0)})
    projects.sort(key=lambda p: (-p["planned"], -p["actual"]))
    tot["outside"] = outside
    tot["work"] = total_work
    tot["adherence"] = tot["on_plan"] / tot["elapsed"] if tot["elapsed"] > 0 else None
    tot["coverage"] = tot["on_plan"] / total_work if total_work > 0 else None
    return {"day": day, "t0": t0, "t1": t1, "in_progress": now < t1, "blocks": blocks, "projects": projects, "totals": tot}


def plan_history(conn, t0, t1):
    """Per-day adherence for every planned day in the range (for trends)."""
    days = [r["day"] for r in conn.execute("SELECT DISTINCT day FROM plan_blocks WHERE start < ? AND end > ? ORDER BY day", (t1, t0))]
    out = []
    for day in days:
        r = plan_review(conn, day)
        t = r["totals"]
        out.append({"day": day, "weekday": parse_day(day).weekday(), "planned": t["planned"], "elapsed": t["elapsed"],
                    "on_plan": t["on_plan"], "missed": max(0.0, t["elapsed"] - t["on_plan"]), "off_plan": t["off_plan"],
                    "outside": t["outside"], "adherence": t["adherence"], "blocks": len(r["blocks"])})
    return out


def plan_totals_for_range(conn, t0, t1, segs, now=None):
    """Lightweight planned / on-plan totals for the Overview tile (segments already labelled)."""
    now = now or time.time()
    blocks = rows(conn, "SELECT * FROM plan_blocks WHERE start < ? AND end > ? ORDER BY start", (t1, t0))
    if not blocks:
        return None
    work = [s for s in segs if is_work(s)]
    planned = elapsed = on_plan = 0.0
    for b in blocks:
        planned += b["end"] - b["start"]
        elapsed += max(0.0, min(b["end"], now) - b["start"])
        for s in work:
            if b["project_id"] and s.get("project_id") == b["project_id"]:
                o = _overlap(s["start"], s["end"], b["start"], b["end"])
                if o > 0:
                    on_plan += o
    return {"planned": planned, "elapsed": elapsed, "on_plan": on_plan, "days": len({b["day"] for b in blocks}),
            "adherence": on_plan / elapsed if elapsed > 0 else None}


# -- all-time project totals (per-day cache) and estimates ---------------------

import hashlib
import threading
import time

_day_cache = {}
_cache_lock = threading.Lock()


def _config_fingerprint(conn, settings):
    """Changes whenever anything that affects attribution changes; cosmetic edits don't count."""
    h = hashlib.sha1()
    for sql in ("SELECT id, field, pattern, is_regex, project_id, task_id, category, priority FROM rules ORDER BY id",
                "SELECT id, start, end, bundle, project_id, task_id, category FROM assignments ORDER BY id",
                "SELECT id, start, end, project_id, task_id, category FROM manual_entries ORDER BY id",
                "SELECT id, category FROM projects ORDER BY id",
                "SELECT id, project_id FROM tasks ORDER BY id"):
        for row in conn.execute(sql):
            h.update(repr(tuple(row)).encode())
    for k in ("cs_max_seconds", "cs_min_count", "cs_min_distinct"):
        h.update(str(settings.get(k)).encode())
    return h.hexdigest()


def project_daily_totals(conn, settings=None):
    """{day: {project_id: seconds, ('task', task_id): seconds}} over all history.
    Past days are cached in-process (keyed by the attribution config), so only today is recomputed."""
    settings = settings or get_settings(conn)
    fp = _config_fingerprint(conn, settings)
    first = conn.execute("SELECT min(s) FROM (SELECT min(start) AS s FROM segments UNION ALL SELECT min(start) FROM manual_entries)").fetchone()[0]
    if first is None:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    day = datetime.fromtimestamp(first).strftime("%Y-%m-%d")
    clf, out = None, {}
    with _cache_lock:
        while day <= today:
            cached = _day_cache.get(day)
            if cached and cached[0] == fp and day != today:
                totals = cached[1]
            else:
                t0, t1 = day_bounds(day)
                segs = apply_manual_entries(load_segments(conn, t0, t1), load_manual(conn, t0, t1))
                totals = {}
                if segs:
                    if clf is None:
                        clf = Classifier(conn, settings)
                    clf.classify(segs)
                    for s in segs:
                        if not is_work(s) or not s.get("project_id"):
                            continue
                        d = s["end"] - s["start"]
                        totals[s["project_id"]] = totals.get(s["project_id"], 0.0) + d
                        if s.get("task_id"):
                            k = ("task", s["task_id"])
                            totals[k] = totals.get(k, 0.0) + d
                _day_cache[day] = (fp, totals)
            if totals:
                out[day] = totals
            day = (parse_day(day) + timedelta(days=1)).strftime("%Y-%m-%d")
    return out


def _estimate_block(seconds, estimate_hours, done):
    est = estimate_hours * 3600 if estimate_hours else None
    b = {"seconds": seconds, "estimate_seconds": est, "ratio": (seconds / est) if est else None}
    if est and not done:
        b["remaining"] = max(0.0, est - seconds)
    return b


def project_stats(conn):
    settings = get_settings(conn)
    daily = project_daily_totals(conn, settings)
    projects = rows(conn, "SELECT * FROM projects ORDER BY done, archived, lower(title)")
    tasks = rows(conn, "SELECT * FROM tasks ORDER BY done, id")
    calibration = []
    for p in projects:
        pid = p["id"]
        days = sorted(d for d, t in daily.items() if t.get(pid))
        total = sum(daily[d][pid] for d in days)
        recent = days[-10:]
        pace = (sum(daily[d][pid] for d in recent) / len(recent)) if recent else 0.0
        st = _estimate_block(total, p["estimate_hours"], p["done"])
        st.update(active_days=len(days), first_day=days[0] if days else None, last_day=days[-1] if days else None, pace=pace)
        if st.get("remaining") and pace > 0:
            st["projected_days"] = st["remaining"] / pace
        p["stats"] = st
        p["tasks"] = []
        for t in tasks:
            if t["project_id"] != pid:
                continue
            tsecs = sum(dt.get(("task", t["id"]), 0.0) for dt in daily.values())
            t["stats"] = _estimate_block(tsecs, t["estimate_hours"], t["done"])
            p["tasks"].append(t)
            if t["done"] and t["estimate_hours"]:
                calibration.append({"kind": "task", "id": t["id"], "title": t["title"], "project": p["title"], "project_id": pid,
                                    "estimate_seconds": t["estimate_hours"] * 3600, "actual_seconds": tsecs,
                                    "ratio": tsecs / (t["estimate_hours"] * 3600), "completed_at": t["completed_at"]})
        if p["done"] and p["estimate_hours"]:
            calibration.append({"kind": "project", "id": pid, "title": p["title"], "project": p["title"], "project_id": pid,
                                "estimate_seconds": p["estimate_hours"] * 3600, "actual_seconds": total,
                                "ratio": total / (p["estimate_hours"] * 3600), "completed_at": p["completed_at"]})
    calibration.sort(key=lambda c: -(c["completed_at"] or 0))
    ratios = sorted(c["ratio"] for c in calibration)
    summary = None
    if ratios:
        n = len(ratios)
        median = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
        summary = {"count": n, "mean_ratio": sum(ratios) / n, "median_ratio": median,
                   "under": sum(1 for r in ratios if r > 1.1), "over": sum(1 for r in ratios if r < 0.9),
                   "total_estimated": sum(c["estimate_seconds"] for c in calibration),
                   "total_actual": sum(c["actual_seconds"] for c in calibration)}
    return {"projects": projects, "calibration": calibration, "calibration_summary": summary}

