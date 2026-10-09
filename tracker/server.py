"""Local-only HTTP server: JSON API + static dashboard. Binds 127.0.0.1 only."""
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, analytics, db, focus, notify, reflect
from .analytics import Filters, parse_time, range_bounds
from .classify import BROWSERS, CATEGORIES, FIELD_SPECIFICITY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
ROUTES = []


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class Raw:
    def __init__(self, data, ctype, status=200, headers=None):
        self.data, self.ctype, self.status, self.headers = data, ctype, status, headers or {}


def route(method, pattern):
    rx = re.compile(pattern)

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


def today():
    return datetime.now().strftime("%Y-%m-%d")


def range_from_query(q):
    frm = q.get("from") or today()
    to = q.get("to") or frm
    try:
        return range_bounds(frm, to)
    except ValueError:
        raise ApiError("dates must be YYYY-MM-DD")


def require(body, *keys):
    body = body or {}
    for k in keys:
        if body.get(k) in (None, ""):
            raise ApiError(f"'{k}' is required")
    return body


def opt_int(v):
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError("expected an integer id")


def opt_category(v, allow_none=True):
    if v in (None, ""):
        if allow_none:
            return None
        raise ApiError("category is required")
    if v not in CATEGORIES:
        raise ApiError("category must be productive, neutral or distracting")
    return v


def opt_hours(v):
    if v in (None, "", "null"):
        return None
    try:
        h = float(v)
    except (TypeError, ValueError):
        raise ApiError("estimate must be a number of hours")
    if h < 0:
        raise ApiError("estimate must be positive")
    return h


def apply_done(body):
    """Translate a 'done' flag into done + completed_at columns."""
    if "done" in body:
        done = 1 if body["done"] else 0
        body["done"] = done
        body["completed_at"] = time.time() if done else None
    return body


def daemon_running():
    try:
        return subprocess.run(["pgrep", "-x", "TrackerDaemon"], capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def _update(conn, table, row_id, body, allowed):
    sets, vals = [], []
    for k in allowed:
        if k in body:
            sets.append(f"{k} = ?")
            vals.append(body[k])
    if not sets:
        raise ApiError("nothing to update")
    vals.append(row_id)
    cur = conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", vals)
    if cur.rowcount == 0:
        raise ApiError("not found", 404)
    conn.commit()
    return db.one(conn, f"SELECT * FROM {table} WHERE id = ?", (row_id,))


def _delete(conn, table, row_id):
    cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise ApiError("not found", 404)
    return {"ok": True}


# -- status / data -------------------------------------------------------------

@route("GET", r"/api/status")
def api_status(conn, q, body):
    now = time.time()
    last = db.one(conn, "SELECT * FROM segments ORDER BY id DESC LIMIT 1")
    recent = now - 900
    placeholders = ",".join("?" * len(BROWSERS))
    counts = db.one(conn, f"""
        SELECT
          sum(kind='active' AND end > ?) AS active_recent,
          sum(kind='active' AND end > ? AND title IS NOT NULL) AS titles_recent,
          sum(kind='active' AND end > ? AND bundle IN ({placeholders})) AS browser_recent,
          sum(kind='active' AND end > ? AND bundle IN ({placeholders}) AND url IS NOT NULL) AS urls_recent,
          count(*) AS segment_count, min(start) AS first_start
        FROM segments""", (recent, recent, recent, *BROWSERS, recent, *BROWSERS))
    path = db.db_path()
    settings = {k: v for k, v in db.get_settings(conn).items() if k not in focus.SECRET_SETTINGS}
    fstate = focus.effective_state(conn)
    return {
        "version": __version__,
        "now": now,
        "focus": {"active": fstate["active"], "mode": fstate["mode"], "until": fstate["until"], "locked": fstate["locked"],
                  "always": bool(fstate.get("always") or fstate.get("always_apps")), "always_paused": fstate.get("always_paused"),
                  "always_configured": fstate.get("always_configured"), "always_next_change": fstate.get("always_next_change")},
        "daemon_running": daemon_running(),
        "paused": settings.get("paused") == "1",
        "last_segment": last,
        "heartbeat_age": (now - last["end"]) if last else None,
        "counts": {k: (counts[k] or 0) for k in counts},
        "db_path": path,
        "db_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "settings": settings,
        "web_dir": WEB_DIR,
    }


@route("GET", r"/api/summary")
def api_summary(conn, q, body):
    t0, t1 = range_from_query(q)
    return analytics.summary(conn, t0, t1, Filters.from_query(q))


@route("GET", r"/api/timeline")
def api_timeline(conn, q, body):
    day = q.get("date") or today()
    try:
        analytics.parse_day(day)
    except ValueError:
        raise ApiError("date must be YYYY-MM-DD")
    return analytics.timeline(conn, day)


@route("GET", r"/api/unsorted")
def api_unsorted(conn, q, body):
    t0, t1 = range_from_query(q)
    return {"items": analytics.unsorted(conn, t0, t1, int(q.get("limit") or 40))}


@route("GET", r"/api/export\.csv")
def api_export(conn, q, body):
    t0, t1 = range_from_query(q)
    name = f"tracker-{q.get('from') or today()}-to-{q.get('to') or q.get('from') or today()}.csv"
    return Raw(analytics.export_csv(conn, t0, t1).encode("utf-8"), "text/csv; charset=utf-8",
               headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -- projects & tasks ----------------------------------------------------------

def _projects_with_tasks(conn):
    projects = db.rows(conn, "SELECT * FROM projects ORDER BY archived, lower(title)")
    tasks = db.rows(conn, "SELECT * FROM tasks ORDER BY done, id")
    by_pid = {}
    for t in tasks:
        by_pid.setdefault(t["project_id"], []).append(t)
    for p in projects:
        p["tasks"] = by_pid.get(p["id"], [])
    return projects


@route("GET", r"/api/projects")
def api_projects(conn, q, body):
    return {"projects": _projects_with_tasks(conn)}


@route("GET", r"/api/projects/stats")
def api_project_stats(conn, q, body):
    return analytics.project_stats(conn)


@route("POST", r"/api/projects")
def api_project_create(conn, q, body):
    body = require(body, "title")
    cat = opt_category(body.get("category")) or "productive"
    color = body.get("color")
    if color is None:
        used = [r["color"] for r in conn.execute("SELECT color FROM projects WHERE archived = 0")]
        color = next((c for c in range(8) if c not in used), len(used) % 8)
    cur = conn.execute(
        "INSERT INTO projects(title, description, color, category, created_at, estimate_hours) VALUES (?,?,?,?,?,?)",
        (body["title"].strip(), (body.get("description") or "").strip(), int(color), cat, time.time(),
         opt_hours(body.get("estimate_hours"))))
    conn.commit()
    p = db.one(conn, "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,))
    p["tasks"] = []
    return p


@route("PUT", r"/api/projects/(\d+)")
def api_project_update(conn, q, body, pid):
    body = body or {}
    if "category" in body:
        body["category"] = opt_category(body["category"], allow_none=False)
    if "archived" in body:
        body["archived"] = 1 if body["archived"] else 0
    if "estimate_hours" in body:
        body["estimate_hours"] = opt_hours(body["estimate_hours"])
    apply_done(body)
    return _update(conn, "projects", int(pid), body, ("title", "description", "category", "archived", "color", "estimate_hours", "done", "completed_at"))


@route("DELETE", r"/api/projects/(\d+)")
def api_project_delete(conn, q, body, pid):
    # Rules that only pointed at this project would become no-ops; drop them. Rules that also set a
    # category keep working as category-only rules.
    conn.execute("DELETE FROM rules WHERE project_id = ? AND category IS NULL", (int(pid),))
    return _delete(conn, "projects", int(pid))


@route("POST", r"/api/tasks")
def api_task_create(conn, q, body):
    body = require(body, "project_id", "title")
    cur = conn.execute("INSERT INTO tasks(project_id, title, description, created_at, estimate_hours) VALUES (?,?,?,?,?)",
                       (int(body["project_id"]), body["title"].strip(), (body.get("description") or "").strip(), time.time(),
                        opt_hours(body.get("estimate_hours"))))
    conn.commit()
    return db.one(conn, "SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,))


@route("PUT", r"/api/tasks/(\d+)")
def api_task_update(conn, q, body, tid):
    body = dict(body or {})
    if "estimate_hours" in body:
        body["estimate_hours"] = opt_hours(body["estimate_hours"])
    apply_done(body)
    return _update(conn, "tasks", int(tid), body, ("title", "description", "done", "completed_at", "estimate_hours"))


@route("DELETE", r"/api/tasks/(\d+)")
def api_task_delete(conn, q, body, tid):
    return _delete(conn, "tasks", int(tid))


# -- rules ---------------------------------------------------------------------

@route("GET", r"/api/rules")
def api_rules(conn, q, body):
    rules = db.rows(conn, """
        SELECT r.*, p.title AS project_title, t.title AS task_title
        FROM rules r LEFT JOIN projects p ON p.id = r.project_id LEFT JOIN tasks t ON t.id = r.task_id
        ORDER BY r.priority DESC, r.id DESC""")
    for r in rules:
        r["specificity"] = FIELD_SPECIFICITY.get(r["field"], 0)
    return {"rules": rules}


@route("POST", r"/api/rules")
def api_rule_create(conn, q, body):
    body = require(body, "field", "pattern")
    if body["field"] not in FIELD_SPECIFICITY:
        raise ApiError("field must be one of " + ", ".join(FIELD_SPECIFICITY))
    pid, tid = opt_int(body.get("project_id")), opt_int(body.get("task_id"))
    cat = opt_category(body.get("category"))
    if pid is None and tid is None and cat is None:
        raise ApiError("a rule needs a project, a task or a category")
    if tid is not None and pid is None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        pid = t["project_id"] if t else None
    cur = conn.execute(
        "INSERT INTO rules(field, pattern, is_regex, project_id, task_id, category, priority, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (body["field"], body["pattern"].strip(), 1 if body.get("is_regex") else 0, pid, tid, cat,
         int(body.get("priority") or 0), time.time()))
    conn.commit()
    return db.one(conn, "SELECT * FROM rules WHERE id = ?", (cur.lastrowid,))


@route("POST", r"/api/rules/auto")
def api_rules_auto(conn, q, body):
    """'Remember this window': create (or re-point) one rule per identity so the same page/window/workspace
    is attributed automatically from now on (and retroactively, like every rule)."""
    body = body or {}
    idents = body.get("rules") or []
    pid, tid, cat = opt_int(body.get("project_id")), opt_int(body.get("task_id")), opt_category(body.get("category"))
    if pid is None and tid is None and cat is None:
        raise ApiError("choose a project, a task or a category")
    if tid is not None and pid is None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        pid = t["project_id"] if t else None
    created = updated = 0
    for it in idents:
        field, pattern = it.get("field"), (it.get("pattern") or "").strip()
        if field not in FIELD_SPECIFICITY or not pattern:
            continue
        existing = db.one(conn, "SELECT * FROM rules WHERE field = ? AND lower(pattern) = lower(?) AND is_regex = 0", (field, pattern))
        if existing:
            conn.execute("UPDATE rules SET project_id = ?, task_id = ?, category = ? WHERE id = ?", (pid, tid, cat, existing["id"]))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO rules(field, pattern, is_regex, project_id, task_id, category, priority, created_at, origin) VALUES (?,?,0,?,?,?,?,?,'auto')",
                (field, pattern, pid, tid, cat, int(body.get("priority") or 1), time.time()))
            created += 1
    conn.commit()
    return {"created": created, "updated": updated}


@route("PUT", r"/api/rules/(\d+)")
def api_rule_update(conn, q, body, rid):
    body = dict(body or {})
    if "category" in body:
        body["category"] = opt_category(body["category"])
    for k in ("project_id", "task_id"):
        if k in body:
            body[k] = opt_int(body[k])
    if "is_regex" in body:
        body["is_regex"] = 1 if body["is_regex"] else 0
    return _update(conn, "rules", int(rid), body, ("field", "pattern", "is_regex", "project_id", "task_id", "category", "priority"))


@route("DELETE", r"/api/rules/(\d+)")
def api_rule_delete(conn, q, body, rid):
    return _delete(conn, "rules", int(rid))


# -- assignments (relabel tracked time) & manual entries (time away) -----------

def _time_range(body):
    start, end = parse_time(body.get("start")), parse_time(body.get("end"))
    if start is None or end is None:
        raise ApiError("start and end are required")
    if end <= start:
        raise ApiError("end must be after start")
    return start, end


@route("GET", r"/api/assignments")
def api_assignments(conn, q, body):
    t0, t1 = range_from_query(q)
    return {"assignments": db.rows(conn, "SELECT * FROM assignments WHERE end > ? AND start < ? ORDER BY start", (t0, t1))}


@route("POST", r"/api/assignments")
def api_assignment_create(conn, q, body):
    body = body or {}
    start, end = _time_range(body)
    pid, tid, cat = opt_int(body.get("project_id")), opt_int(body.get("task_id")), opt_category(body.get("category"))
    if pid is None and tid is None and cat is None:
        raise ApiError("choose a project, a task or a category")
    if tid is not None and pid is None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        pid = t["project_id"] if t else None
    cur = conn.execute(
        "INSERT INTO assignments(start, end, bundle, project_id, task_id, category, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (start, end, body.get("bundle") or None, pid, tid, cat, (body.get("note") or "").strip(), time.time()))
    conn.commit()
    return db.one(conn, "SELECT * FROM assignments WHERE id = ?", (cur.lastrowid,))


@route("POST", r"/api/assignments/bulk")
def api_assignment_bulk(conn, q, body):
    """One assignment per item (a session's time range + app), all to the same project/task/category."""
    body = body or {}
    items = body.get("items") or []
    if not items:
        raise ApiError("no sessions given")
    pid, tid, cat = opt_int(body.get("project_id")), opt_int(body.get("task_id")), opt_category(body.get("category"))
    if pid is None and tid is None and cat is None:
        raise ApiError("choose a project, a task or a category")
    if tid is not None and pid is None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        pid = t["project_id"] if t else None
    note = (body.get("note") or "").strip()
    now = time.time()
    created = 0
    for it in items:
        start, end = _time_range(it)
        conn.execute(
            "INSERT INTO assignments(start, end, bundle, project_id, task_id, category, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (start, end, it.get("bundle") or None, pid, tid, cat, note, now))
        created += 1
    conn.commit()
    return {"created": created}


@route("DELETE", r"/api/assignments/(\d+)")
def api_assignment_delete(conn, q, body, aid):
    return _delete(conn, "assignments", int(aid))


@route("GET", r"/api/manual")
def api_manual(conn, q, body):
    t0, t1 = range_from_query(q)
    return {"entries": db.rows(conn, "SELECT * FROM manual_entries WHERE end > ? AND start < ? ORDER BY start", (t0, t1))}


@route("POST", r"/api/manual")
def api_manual_create(conn, q, body):
    body = require(body, "title")
    start, end = _time_range(body)
    pid, tid, cat = opt_int(body.get("project_id")), opt_int(body.get("task_id")), opt_category(body.get("category"))
    if tid is not None and pid is None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        pid = t["project_id"] if t else None
    cur = conn.execute(
        "INSERT INTO manual_entries(start, end, title, project_id, task_id, category, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (start, end, body["title"].strip(), pid, tid, cat, (body.get("note") or "").strip(), time.time()))
    conn.commit()
    return db.one(conn, "SELECT * FROM manual_entries WHERE id = ?", (cur.lastrowid,))


@route("PUT", r"/api/manual/(\d+)")
def api_manual_update(conn, q, body, mid):
    body = dict(body or {})
    if "start" in body or "end" in body:
        cur = db.one(conn, "SELECT * FROM manual_entries WHERE id = ?", (int(mid),))
        if not cur:
            raise ApiError("not found", 404)
        merged = {"start": body.get("start", cur["start"]), "end": body.get("end", cur["end"])}
        body["start"], body["end"] = _time_range(merged)
    if "category" in body:
        body["category"] = opt_category(body["category"])
    for k in ("project_id", "task_id"):
        if k in body:
            body[k] = opt_int(body[k])
    return _update(conn, "manual_entries", int(mid), body, ("start", "end", "title", "project_id", "task_id", "category", "note"))


@route("DELETE", r"/api/manual/(\d+)")
def api_manual_delete(conn, q, body, mid):
    return _delete(conn, "manual_entries", int(mid))


# -- daily plan ----------------------------------------------------------------

def _day_arg(v):
    v = v or today()
    try:
        analytics.parse_day(v)
    except (ValueError, TypeError):
        raise ApiError("day must be YYYY-MM-DD")
    return v


def _block_time(day, v, allow_end=False):
    """'HH:MM' relative to the day (24:00 allowed for an end), or unix seconds / ISO."""
    if isinstance(v, str) and re.fullmatch(r"\d{1,2}:\d{2}", v.strip()):
        h, m = (int(x) for x in v.strip().split(":"))
        if not (0 <= m < 60) or h < 0 or h > 24 or (h == 24 and (m or not allow_end)):
            raise ApiError("time must be HH:MM")
        return analytics.day_bounds(day)[0] + h * 3600 + m * 60
    t = parse_time(v)
    if t is None:
        raise ApiError("start and end are required")
    return t


def _block_body(conn, day, body, existing=None):
    body = body or {}
    out = {}
    if "start" in body or "end" in body or existing is None:
        start = _block_time(day, body.get("start", existing["start"] if existing else None))
        end = _block_time(day, body.get("end", existing["end"] if existing else None), allow_end=True)
        t0, t1 = analytics.day_bounds(day)
        if end <= start:
            raise ApiError("end must be after start")
        if start < t0 or end > t1 + 1:
            raise ApiError("block must lie within the day")
        out["start"], out["end"] = start, end
    for k in ("project_id", "task_id"):
        if k in body:
            out[k] = opt_int(body[k])
    if "note" in body:
        out["note"] = (body.get("note") or "").strip()
    pid = out.get("project_id", existing["project_id"] if existing else None)
    tid = out.get("task_id", existing["task_id"] if existing else None)
    if tid is not None:
        t = db.one(conn, "SELECT project_id FROM tasks WHERE id = ?", (tid,))
        if not t:
            raise ApiError("task not found", 404)
        if pid is None:
            out["project_id"] = pid = t["project_id"]
        elif t["project_id"] != pid:
            raise ApiError("task belongs to a different project")
    if pid is None:
        raise ApiError("choose a project for the block")
    return out


@route("GET", r"/api/plan")
def api_plan(conn, q, body):
    return analytics.plan_review(conn, _day_arg(q.get("date")))


@route("GET", r"/api/plan/history")
def api_plan_history(conn, q, body):
    t0, t1 = range_from_query(q)
    return {"days": analytics.plan_history(conn, t0, t1)}


@route("POST", r"/api/plan/blocks")
def api_plan_block_create(conn, q, body):
    body = body or {}
    day = _day_arg(body.get("day"))
    vals = _block_body(conn, day, body)
    cur = conn.execute(
        "INSERT INTO plan_blocks(day, start, end, project_id, task_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (day, vals["start"], vals["end"], vals.get("project_id"), vals.get("task_id"), vals.get("note", ""), time.time()))
    conn.commit()
    return db.one(conn, "SELECT * FROM plan_blocks WHERE id = ?", (cur.lastrowid,))


@route("PUT", r"/api/plan/blocks/(\d+)")
def api_plan_block_update(conn, q, body, bid):
    existing = db.one(conn, "SELECT * FROM plan_blocks WHERE id = ?", (int(bid),))
    if not existing:
        raise ApiError("not found", 404)
    vals = _block_body(conn, existing["day"], body, existing)
    return _update(conn, "plan_blocks", int(bid), vals, ("start", "end", "project_id", "task_id", "note"))


@route("DELETE", r"/api/plan/blocks/(\d+)")
def api_plan_block_delete(conn, q, body, bid):
    return _delete(conn, "plan_blocks", int(bid))


@route("POST", r"/api/plan/copy")
def api_plan_copy(conn, q, body):
    body = body or {}
    src, dst = _day_arg(body.get("from_day")), _day_arg(body.get("to_day"))
    if src == dst:
        raise ApiError("source and target day are the same")
    shift = analytics.day_bounds(dst)[0] - analytics.day_bounds(src)[0]
    blocks = db.rows(conn, "SELECT * FROM plan_blocks WHERE day = ? ORDER BY start", (src,))
    if not blocks:
        raise ApiError(f"no plan on {src}")
    if body.get("replace"):
        conn.execute("DELETE FROM plan_blocks WHERE day = ?", (dst,))
    for b in blocks:
        conn.execute("INSERT INTO plan_blocks(day, start, end, project_id, task_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                     (dst, b["start"] + shift, b["end"] + shift, b["project_id"], b["task_id"], b["note"], time.time()))
    conn.commit()
    return {"copied": len(blocks), "day": dst}


@route("DELETE", r"/api/plan")
def api_plan_clear(conn, q, body):
    day = _day_arg(q.get("date"))
    cur = conn.execute("DELETE FROM plan_blocks WHERE day = ?", (day,))
    conn.commit()
    return {"deleted": cur.rowcount}


# -- focus (website / app blocking) ---------------------------------------------

ENFORCER = None


def _focus_payload(conn):
    focus.close_expired(conn)
    return {
        "state": focus.effective_state(conn),
        "session": focus.active_session(conn),
        "lists": focus.get_lists(conn),
        "distracting": focus.distracting_from_rules(conn),
        "history": focus.history(conn),
        "enforcer": ENFORCER.status if ENFORCER else None,
        "lock": {"set": focus.lock_is_set(conn)},
        "recent_apps": [r["app"] for r in conn.execute(
            "SELECT app, sum(end - start) AS s FROM segments WHERE kind = 'active' AND app IS NOT NULL AND end > ? GROUP BY app ORDER BY s DESC LIMIT 30",
            (time.time() - 14 * 86400,))],
        "network_services": focus.network_services(),
    }


def _enforce_now():
    if ENFORCER:
        ENFORCER.tick()


@route("GET", r"/api/focus")
def api_focus(conn, q, body):
    return _focus_payload(conn)


@route("POST", r"/api/focus/start")
def api_focus_start(conn, q, body):
    body = body or {}
    try:
        focus.start_session(conn, body.get("mode"), body.get("minutes") or 25, bool(body.get("locked")))
    except ValueError as e:
        raise ApiError(str(e))
    conn.commit()
    _enforce_now()
    return _focus_payload(conn)


@route("POST", r"/api/focus/stop")
def api_focus_stop(conn, q, body):
    try:
        focus.stop_session(conn, force=bool((body or {}).get("force")))
    except ValueError as e:
        raise ApiError(str(e), 423)
    _enforce_now()
    return _focus_payload(conn)


@route("POST", r"/api/focus/extend")
def api_focus_extend(conn, q, body):
    try:
        focus.extend_session(conn, (body or {}).get("minutes") or 15)
    except ValueError as e:
        raise ApiError(str(e))
    _enforce_now()
    return _focus_payload(conn)


@route("POST", r"/api/focus/sites")
def api_focus_site_add(conn, q, body):
    body = body or {}
    try:
        row = focus.add_site(conn, body.get("list"), body.get("pattern"))
    except ValueError as e:
        raise ApiError(str(e))
    _enforce_now()
    return row


@route("DELETE", r"/api/focus/sites/(\d+)")
def api_focus_site_delete(conn, q, body, sid):
    row = db.one(conn, "SELECT * FROM focus_sites WHERE id = ?", (int(sid),))
    if not row:
        raise ApiError("not found", 404)
    if row["list"] in focus.ALWAYS_LISTS:
        try:
            focus.require_password(conn, (body or {}).get("password") or q.get("password") or "")
        except focus.TooManyAttempts as e:
            raise ApiError(str(e), 429)
        except PermissionError as e:
            raise ApiError(str(e), 401)
    out = _delete(conn, "focus_sites", int(sid))
    _enforce_now()
    return out


@route("PUT", r"/api/focus/always-hours")
def api_focus_always_hours(conn, q, body):
    """Hours during which the always list is not blocked. Protected by the lock password when one is set."""
    body = body or {}
    try:
        focus.require_password(conn, body.get("password") or "")
        windows = focus.normalize_windows(body.get("windows") or [])
    except focus.TooManyAttempts as e:
        raise ApiError(str(e), 429)
    except PermissionError as e:
        raise ApiError(str(e), 401)
    except (ValueError, TypeError, AttributeError) as e:
        raise ApiError(str(e))
    db.set_setting(conn, "always_allow_windows", json.dumps(windows))
    _enforce_now()
    return _focus_payload(conn)


@route("POST", r"/api/focus/lock")
def api_focus_lock_set(conn, q, body):
    body = body or {}
    try:
        focus.set_lock(conn, body.get("password") or "", body.get("current") or "")
    except focus.TooManyAttempts as e:
        raise ApiError(str(e), 429)
    except PermissionError as e:
        raise ApiError(str(e), 401)
    except ValueError as e:
        raise ApiError(str(e))
    return {"set": True}


@route("DELETE", r"/api/focus/lock")
def api_focus_lock_clear(conn, q, body):
    try:
        focus.clear_lock(conn, (body or {}).get("password") or "")
    except focus.TooManyAttempts as e:
        raise ApiError(str(e), 429)
    except PermissionError as e:
        raise ApiError(str(e), 401)
    return {"set": False}


@route("GET", r"/focus\.pac")
def focus_pac(conn, q, body):
    focus.close_expired(conn)
    return Raw(focus.pac_text(focus.effective_state(conn)).encode("utf-8"), "application/x-ns-proxy-autoconfig")


class BlockHandler(BaseHTTPRequestHandler):
    """The proxy that blocked hosts are routed to. It refuses everything: 403 on CONNECT (HTTPS) and a
    block page for plain HTTP. It never forwards traffic."""
    protocol_version = "HTTP/1.1"
    server_version = f"TrackerFocus/{__version__}"

    def log_message(self, fmt, *args):
        pass

    def _state(self):
        try:
            with closing(db.connect()) as conn:
                return focus.effective_state(conn)
        except Exception:  # noqa: BLE001
            return {"active": True, "mode": "?", "until": None}

    def do_CONNECT(self):
        self.send_response(403, "Blocked by Tracker focus")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _block(self):
        page = focus.block_page(self._state(), self.path).encode("utf-8")
        self.send_response(403, "Blocked by Tracker focus")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(page)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _block


def start_focus_services(dashboard_port):
    """Blocking proxy + enforcement loop, both as daemon threads inside the dashboard process."""
    global ENFORCER
    with closing(db.connect()) as conn:
        proxy_port = int(db.get_settings(conn).get("focus_proxy_port") or 7897)
    ENFORCER = focus.Enforcer(dashboard_port)
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        proxy = ThreadingHTTPServer(("127.0.0.1", proxy_port), BlockHandler)
        proxy.daemon_threads = True
        threading.Thread(target=proxy.serve_forever, name="focus-proxy", daemon=True).start()
    except OSError as e:
        print(f"focus proxy could not listen on 127.0.0.1:{proxy_port}: {e}", flush=True)

    def loop():
        while True:
            ENFORCER.tick()
            time.sleep(3 if ENFORCER.state.get("active") else 5)
    threading.Thread(target=loop, name="focus-enforcer", daemon=True).start()
    return ENFORCER


# -- reflections & notifications -------------------------------------------------

NOTIFIER = None


def _reflection_payload(conn, day):
    entry = reflect.get(conn, day)
    return {"day": day, "entry": entry, "stats": reflect.day_stats(conn, day), "path": os.path.join(reflect.export_dir(), f"{day}.md") if entry else None}


@route("GET", r"/api/reflections")
def api_reflections(conn, q, body):
    try:
        rows = reflect.list_entries(conn, q.get("since") or None, q.get("limit") or None)
    except ValueError as e:
        raise ApiError(str(e))
    return {"entries": rows, "dir": reflect.export_dir()}


@route("GET", r"/api/reflections/(\d{4}-\d{2}-\d{2})")
def api_reflection(conn, q, body, day):
    try:
        return _reflection_payload(conn, day)
    except ValueError as e:
        raise ApiError(str(e))


@route("PUT", r"/api/reflections/(\d{4}-\d{2}-\d{2})")
def api_reflection_save(conn, q, body, day):
    body = body or {}
    try:
        reflect.save(conn, day, body.get("text") or "", body.get("rating"))
    except ValueError as e:
        raise ApiError(str(e))
    return _reflection_payload(conn, day)


@route("DELETE", r"/api/reflections/(\d{4}-\d{2}-\d{2})")
def api_reflection_delete(conn, q, body, day):
    if not reflect.delete(conn, day):
        raise ApiError("not found", 404)
    return {"ok": True}


@route("POST", r"/api/notify/test")
def api_notify_test(conn, q, body):
    ok = notify.send("Tracker", (body or {}).get("message") or "Notifications are working.", "Test")
    if not ok:
        raise ApiError("could not post a notification (osascript failed)", 500)
    return {"ok": True}


# -- settings / control --------------------------------------------------------

def _public_settings(conn):
    return {k: v for k, v in db.get_settings(conn).items() if k not in focus.SECRET_SETTINGS}


@route("GET", r"/api/settings")
def api_settings(conn, q, body):
    return _public_settings(conn)


@route("PUT", r"/api/settings")
def api_settings_update(conn, q, body):
    body = body or {}
    for k, v in body.items():
        if k not in db.DEFAULT_SETTINGS:
            raise ApiError(f"unknown setting '{k}'")
        if k in focus.PROTECTED_SETTINGS:
            raise ApiError(f"'{k}' can only be changed on the Focus tab (it may need the lock password)")
        db.set_setting(conn, k, "" if v is None else str(v))
    return _public_settings(conn)


@route("POST", r"/api/pause")
def api_pause(conn, q, body):
    paused = bool((body or {}).get("paused"))
    db.set_setting(conn, "paused", "1" if paused else "0")
    return {"paused": paused}


def run_backup(conn, directory=None):
    directory = directory or db.get_settings(conn).get("backup_dir") or ""
    if not directory:
        raise ApiError("no backup directory configured")
    directory = os.path.expanduser(directory)
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, "tracker-backup.db")
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn.execute("VACUUM INTO ?", (tmp,))
    os.replace(tmp, dest)
    db.set_setting(conn, "last_backup", str(int(time.time())))
    return {"ok": True, "path": dest, "bytes": os.path.getsize(dest)}


@route("POST", r"/api/backup")
def api_backup(conn, q, body):
    return run_backup(conn, (body or {}).get("dir"))


# -- plumbing ------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"Tracker/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("TRACKER_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status, data, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, status=200):
        self._send(status, json.dumps(obj, default=_json_default).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            q = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
            body = None
            if method in ("POST", "PUT", "DELETE"):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    raise ApiError("invalid JSON body")
            for m, rx, fn in ROUTES:
                if m != method:
                    continue
                mo = rx.fullmatch(path)
                if not mo:
                    continue
                with closing(db.connect()) as conn:
                    result = fn(conn, q, body, *mo.groups())
                if isinstance(result, Raw):
                    self._send(result.status, result.data, result.ctype, result.headers)
                else:
                    self.send_json(result)
                return
            if method == "GET" and not path.startswith("/api/"):
                return self.serve_static(path)
            raise ApiError("not found", 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError("not found", 404)
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


def serve(port=None, host="127.0.0.1"):
    if port is None:
        with closing(db.connect()) as conn:
            port = int(db.get_settings(conn).get("dashboard_port") or 7898)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"tracker dashboard listening on http://{host}:{port}/  (db: {db.db_path()})", flush=True)
    start_focus_services(port)
    global NOTIFIER
    NOTIFIER = notify.start_notifier()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
