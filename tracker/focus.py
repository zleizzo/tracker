"""Focus sessions: block websites (and quit apps) for a set period.

How blocking works: for the duration of a session the dashboard installs a proxy auto-config (PAC)
URL on every network service via `networksetup` (admin users can do this without sudo). Browsers and
most apps evaluate the PAC for every request, so DNS caches, already-resolved IPs, incognito windows,
Secure DNS and typed URLs make no difference: blocked hosts are routed to a local proxy on
127.0.0.1 that refuses them; everything else goes DIRECT and never touches the proxy. The settings
are re-applied every few seconds while a session runs (turning them off in System Settings does not
help) and restored when it ends. Blocked apps are quit every few seconds while a session runs.

Modes:
  blacklist    your own list of sites (and apps) is blocked
  distracting  the blacklist plus every site/app that a rule sorts as "distracting"
  whitelist    only sites on your allow list can be reached; blocked apps still apply

The "always" lists (sites and apps) are enforced whenever the dashboard is running, session or not,
and win over the whitelist.
"""
import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

from . import db

MODES = ("blacklist", "distracting", "whitelist")
LISTS = ("block", "allow", "app", "always", "alwaysapp")
PAC_PATH = "/focus.pac"
# Hosts that must always work (local dashboard, local network, Apple system services in whitelist mode).
ALWAYS_DIRECT = ["localhost", "127.0.0.1", "apple.com", "icloud.com", "icloud-content.com", "apple-dns.net",
                 "push.apple.com", "mzstatic.com", "cdn-apple.com", "gstatic.com"]
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# -- lists ---------------------------------------------------------------------

def normalize_domain(value):
    """'https://www.YouTube.com/watch?v=1' -> 'youtube.com'. Returns None if it is not a domain."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if "://" in v or "/" in v:
        try:
            v = urlsplit(v if "://" in v else "https://" + v).hostname or ""
        except ValueError:
            return None
    v = v.strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v if _DOMAIN_RE.match(v) else None


def get_lists(conn):
    out = {k: [] for k in LISTS}
    for r in conn.execute("SELECT * FROM focus_sites ORDER BY lower(pattern)"):
        if r["list"] in out:
            out[r["list"]].append(dict(r))
    return out


def add_site(conn, which, pattern):
    if which not in LISTS:
        raise ValueError("list must be one of " + ", ".join(LISTS))
    if which in ("app", "alwaysapp"):
        value = (pattern or "").strip()
        if value.lower().endswith(".app"):
            value = value[:-4]
    else:
        value = normalize_domain(pattern)
    if not value:
        raise ValueError("that does not look like a domain" if which not in ("app", "alwaysapp") else "app name is required")
    existing = conn.execute("SELECT id FROM focus_sites WHERE list = ? AND lower(pattern) = lower(?)", (which, value)).fetchone()
    if existing:
        return dict(conn.execute("SELECT * FROM focus_sites WHERE id = ?", (existing["id"],)).fetchone())
    cur = conn.execute("INSERT INTO focus_sites(list, pattern, created_at) VALUES (?,?,?)", (which, value, time.time()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM focus_sites WHERE id = ?", (cur.lastrowid,)).fetchone())


def distracting_from_rules(conn):
    """Domains and app names that your rules sort as distracting."""
    domains, apps = [], []
    for r in conn.execute("SELECT field, pattern, is_regex FROM rules WHERE category = 'distracting'"):
        if r["is_regex"]:
            continue
        if r["field"] in ("domain", "url"):
            d = normalize_domain(r["pattern"])
            if d and d not in domains:
                domains.append(d)
        elif r["field"] == "app":
            a = r["pattern"].strip()
            if a and a not in apps:
                apps.append(a)
    return {"domains": sorted(domains), "apps": sorted(apps)}


# -- sessions ------------------------------------------------------------------

def active_session(conn, now=None):
    now = now or time.time()
    r = conn.execute("SELECT * FROM focus_sessions WHERE stopped_at IS NULL AND end > ? ORDER BY id DESC LIMIT 1", (now,)).fetchone()
    return dict(r) if r else None


def close_expired(conn, now=None):
    now = now or time.time()
    conn.execute("UPDATE focus_sessions SET stopped_at = end WHERE stopped_at IS NULL AND end <= ?", (now,))
    conn.commit()


def start_session(conn, mode, minutes, locked=False):
    if mode not in MODES:
        raise ValueError("mode must be blacklist, distracting or whitelist")
    minutes = float(minutes)
    if not (1 <= minutes <= 24 * 60):
        raise ValueError("duration must be between 1 minute and 24 hours")
    if active_session(conn):
        raise ValueError("a focus session is already running")
    now = time.time()
    cur = conn.execute("INSERT INTO focus_sessions(mode, start, end, locked, created_at) VALUES (?,?,?,?,?)",
                       (mode, now, now + minutes * 60, 1 if locked else 0, now))
    conn.commit()
    return dict(conn.execute("SELECT * FROM focus_sessions WHERE id = ?", (cur.lastrowid,)).fetchone())


def stop_session(conn, force=False):
    s = active_session(conn)
    if not s:
        return None
    if s["locked"] and not force:
        raise ValueError("this session is locked until %s" % time.strftime("%H:%M", time.localtime(s["end"])))
    conn.execute("UPDATE focus_sessions SET stopped_at = ? WHERE id = ?", (time.time(), s["id"]))
    conn.commit()
    return s


def extend_session(conn, minutes):
    s = active_session(conn)
    if not s:
        raise ValueError("no focus session is running")
    minutes = float(minutes)
    if not (1 <= minutes <= 24 * 60):
        raise ValueError("extension must be between 1 minute and 24 hours")
    conn.execute("UPDATE focus_sessions SET end = end + ? WHERE id = ?", (minutes * 60, s["id"]))
    conn.commit()
    return active_session(conn)


def history(conn, limit=30):
    rows = [dict(r) for r in conn.execute("SELECT * FROM focus_sessions ORDER BY start DESC LIMIT ?", (limit,))]
    for r in rows:
        actual_end = r["stopped_at"] if r["stopped_at"] is not None else min(r["end"], time.time())
        r["planned_seconds"] = r["end"] - r["start"]
        r["actual_seconds"] = max(0.0, actual_end - r["start"])
        r["stopped_early"] = r["stopped_at"] is not None and r["stopped_at"] < r["end"] - 1
    return rows


# -- effective block state -----------------------------------------------------

def effective_state(conn):
    """What is blocked right now, as the PAC generator and the app killer see it."""
    lists = get_lists(conn)
    session = active_session(conn)
    settings = db.get_settings(conn)
    always = [r["pattern"] for r in lists["always"]]
    always_apps = [r["pattern"] for r in lists["alwaysapp"]]
    state = {"active": bool(session) or bool(always or always_apps), "mode": None, "until": None, "session_id": None,
             "locked": False, "block": [], "allow": [], "apps": list(always_apps), "always": always, "always_apps": always_apps,
             "proxy_port": int(settings.get("focus_proxy_port") or 7897), "updated": time.time()}
    if not session:
        if state["active"]:
            state["mode"] = "always"
        return state
    block = [r["pattern"] for r in lists["block"]]
    apps = [r["pattern"] for r in lists["app"]]
    if session["mode"] == "distracting":
        extra = distracting_from_rules(conn)
        block = sorted(set(block) | set(extra["domains"]))
        apps = sorted(set(apps) | set(extra["apps"]))
    state.update(mode=session["mode"], until=session["end"], session_id=session["id"], locked=bool(session["locked"]),
                 block=block, allow=[r["pattern"] for r in lists["allow"]], apps=sorted(set(apps) | set(always_apps)))
    return state


def pac_text(state):
    """Proxy auto-config: blocked hosts -> local proxy (which refuses them); everything else DIRECT."""
    if not state.get("active"):
        return 'function FindProxyForURL(url, host) { return "DIRECT"; }\n'
    proxy = "PROXY 127.0.0.1:%d" % state["proxy_port"]
    block = json.dumps(sorted(set(state["block"])))
    allow = json.dumps(sorted(set(state["allow"]) | set(ALWAYS_DIRECT)))
    always = json.dumps(sorted(set(state.get("always") or [])))
    mode = state["mode"]
    until = time.strftime("%H:%M", time.localtime(state["until"])) if state.get("until") else "removed from the always list"
    return f'''// Tracker focus ({mode}) until {until}
var ALWAYS = {always};
var BLOCK = {block};
var ALLOW = {allow};
var MODE = "{mode}";
function matches(host, list) {{
  for (var i = 0; i < list.length; i++) {{
    var d = list[i];
    if (host == d || dnsDomainIs(host, "." + d)) return true;
  }}
  return false;
}}
function FindProxyForURL(url, host) {{
  host = host.toLowerCase();
  if (isPlainHostName(host) || dnsDomainIs(host, ".local") || host == "localhost") return "DIRECT";
  if (/^(127\\.|10\\.|192\\.168\\.|169\\.254\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|\\[?::1\\]?$|fe80:|fd)/.test(host)) return "DIRECT";
  if (matches(host, ALWAYS)) return "{proxy}";
  if (MODE == "always") return "DIRECT";
  if (MODE == "whitelist") return matches(host, ALLOW) ? "DIRECT" : "{proxy}";
  return matches(host, BLOCK) ? "{proxy}" : "DIRECT";
}}
'''


def block_page(state, url=""):
    until = time.strftime("%H:%M", time.localtime(state["until"])) if state.get("until") else "you remove it from the always-blocked list"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Blocked by Tracker</title>
<style>body{{font:16px/1.5 system-ui,-apple-system,sans-serif;background:#f9f9f7;color:#0b0b0b;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.b{{max-width:460px;padding:32px;background:#fcfcfb;border:1px solid rgba(11,11,11,.1);border-radius:12px}}h1{{font-size:22px;margin:0 0 8px}}p{{margin:6px 0;color:#52514e}}code{{background:#f3f2ee;padding:2px 6px;border-radius:4px}}</style></head>
<body><div class="b"><h1>{"Always blocked" if state.get("mode") == "always" else "Blocked during focus"}</h1><p>Mode: <b>{state.get('mode')}</b> · until <b>{until}</b></p><p><code>{url[:120]}</code></p>
<p>Manage the session at <a href="http://127.0.0.1:7898/#focus">the dashboard</a>.</p></div></body></html>"""


# -- applying the proxy setting (networksetup) --------------------------------

def _run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def network_services():
    try:
        out = _run(["networksetup", "-listallnetworkservices"]).stdout.splitlines()
    except Exception:
        return []
    return [l.strip() for l in out[1:] if l.strip() and not l.startswith("*")]


def get_autoproxy(service):
    out = _run(["networksetup", "-getautoproxyurl", service]).stdout
    url = re.search(r"URL:\s*(.*)", out)
    enabled = re.search(r"Enabled:\s*(.*)", out)
    u = (url.group(1).strip() if url else "")
    return {"url": "" if u in ("(null)", "") else u, "enabled": bool(enabled and enabled.group(1).strip().lower() == "yes")}


def prev_path():
    return os.path.join(db.data_dir(), "focus-prev-proxy.json")


def _load_prev():
    try:
        with open(prev_path()) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_prev(prev):
    os.makedirs(db.data_dir(), exist_ok=True)
    with open(prev_path(), "w") as fh:
        json.dump(prev, fh)


def apply_pac(pac_url, is_ours, services=None):
    """Point every enabled network service at our PAC, remembering any foreign config that was there."""
    services = services if services is not None else network_services()
    prev = _load_prev()
    changed = []
    for svc in services:
        cur = get_autoproxy(svc)
        if cur["url"] == pac_url and cur["enabled"]:
            continue
        if svc not in prev and not is_ours(cur["url"]):
            prev[svc] = cur
        _run(["networksetup", "-setautoproxyurl", svc, pac_url])
        _run(["networksetup", "-setautoproxystate", svc, "on"])
        changed.append(svc)
    if changed:
        _save_prev(prev)
        _run(["dscacheutil", "-flushcache"])
    return changed


def clear_pac(is_ours, services=None):
    """Undo apply_pac: restore a previous PAC if there was one, otherwise switch ours off.
    (macOS refuses an empty PAC URL, so a disabled leftover URL is the best we can do.)"""
    services = services if services is not None else network_services()
    prev = _load_prev()
    changed = []
    for svc in services:
        cur = get_autoproxy(svc)
        if not is_ours(cur["url"]):
            continue
        before = prev.get(svc, {"url": "", "enabled": False})
        if before.get("url"):
            _run(["networksetup", "-setautoproxyurl", svc, before["url"]])
            _run(["networksetup", "-setautoproxystate", svc, "on" if before.get("enabled") else "off"])
        elif cur["enabled"]:
            _run(["networksetup", "-setautoproxystate", svc, "off"])
        else:
            continue
        changed.append(svc)
    if os.path.exists(prev_path()):
        os.remove(prev_path())
    if changed:
        _run(["dscacheutil", "-flushcache"])
    return changed


def effective_proxy_ok(pac_url):
    """One cheap call: is the system's current (primary) proxy config our PAC, enabled?"""
    try:
        out = _run(["scutil", "--proxy"], timeout=5).stdout
    except Exception:
        return False
    enabled = re.search(r"ProxyAutoConfigEnable\s*:\s*1", out)
    url = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", out)
    return bool(enabled and url and url.group(1) == pac_url)


def applied_services(is_ours, services=None):
    services = services if services is not None else network_services()
    out = []
    for svc in services:
        cur = get_autoproxy(svc)
        if is_ours(cur["url"]) and cur["enabled"]:
            out.append(svc)
    return out


# -- blocked apps ----------------------------------------------------------------

def quit_apps(names):
    """Terminate running copies of the named apps (by bundle path, then by process name)."""
    killed = []
    for name in names:
        n = name.strip()
        if not n:
            continue
        r1 = _run(["pkill", "-f", f"/{n}.app/Contents/MacOS/"])
        r2 = _run(["killall", "-q", n])
        if r1.returncode == 0 or r2.returncode == 0:
            killed.append(n)
    return killed


# -- enforcement loop (runs inside the dashboard server) -------------------------

import hashlib


class Enforcer:
    """Every few seconds while a session runs: keep the PAC applied on every network service and quit
    blocked apps. When no session runs, make sure nothing of ours is left applied. The PAC URL carries
    a version of the block state so browsers re-fetch it whenever the lists or the session change."""

    def __init__(self, dashboard_port):
        self.base = f"http://127.0.0.1:{dashboard_port}{PAC_PATH}"
        self.dry_run = bool(os.environ.get("TRACKER_FOCUS_DRY_RUN"))
        self.status = {"applied": False, "services": [], "last_check": None, "last_error": None, "killed": [],
                       "dry_run": self.dry_run, "pac_url": None}
        self.state = {"active": False}
        self._checked_idle = False

    def is_ours(self, url):
        return bool(url) and url.startswith(self.base)

    def pac_url(self, state):
        v = hashlib.sha1(json.dumps([state.get("session_id"), state.get("mode"), state.get("until"), state.get("block"), state.get("allow"), state.get("always")], sort_keys=True).encode()).hexdigest()[:10]
        return f"{self.base}?v={v}"

    def tick(self):
        try:
            with db.connect() as conn:
                close_expired(conn)
                self.state = effective_state(conn)
            if self.state["active"]:
                url = self.pac_url(self.state)
                self.status["pac_url"] = url
                if self.dry_run:
                    self.status.update(applied=True, services=["(dry run)"])
                else:
                    # Steady state costs one scutil call; the per-service walk only runs when something is off.
                    if not effective_proxy_ok(url) or not self.status.get("services"):
                        apply_pac(url, self.is_ours)
                        self.status["services"] = applied_services(self.is_ours)
                    self.status["applied"] = bool(self.status["services"])
                    if self.state["apps"]:
                        killed = quit_apps(self.state["apps"])
                        if killed:
                            self.status["killed"] = killed
                self._checked_idle = False
            else:
                self.status["pac_url"] = None
                if not self.dry_run and (self.status.get("applied") or not self._checked_idle):
                    clear_pac(self.is_ours)
                    self._checked_idle = True
                self.status.update(applied=False, services=[])
            self.status["last_error"] = None
        except Exception as e:  # noqa: BLE001
            self.status["last_error"] = f"{type(e).__name__}: {e}"
        self.status["last_check"] = time.time()
        return self.status


# -- lock password for the always-blocked lists --------------------------------

import hmac as _hmac
import secrets as _secrets

SECRET_SETTINGS = {"always_lock_hash", "always_lock_salt"}
ALWAYS_LISTS = ("always", "alwaysapp")
_PBKDF2_ROUNDS = 200_000
_fail_times = []


def lock_is_set(conn):
    r = conn.execute("SELECT value FROM settings WHERE key = 'always_lock_hash'").fetchone()
    return bool(r and r[0])


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS).hex()


def set_lock(conn, password, current=None):
    """Set or change the password. Changing requires the current one."""
    if lock_is_set(conn) and not verify_password(conn, current or ""):
        raise PermissionError("current password is wrong")
    if not password or len(password) < 4:
        raise ValueError("use at least 4 characters")
    salt = _secrets.token_bytes(16)
    db.set_setting(conn, "always_lock_salt", salt.hex())
    db.set_setting(conn, "always_lock_hash", _hash(password, salt))


def clear_lock(conn, password):
    if not verify_password(conn, password or ""):
        raise PermissionError("password is wrong")
    conn.execute("DELETE FROM settings WHERE key IN ('always_lock_hash', 'always_lock_salt')")
    conn.commit()


class TooManyAttempts(Exception):
    pass


def verify_password(conn, password):
    """Constant-time check with a small throttle: 5 failures per minute, half a second per failure."""
    now = time.time()
    while _fail_times and _fail_times[0] < now - 60:
        _fail_times.pop(0)
    if len(_fail_times) >= 5:
        raise TooManyAttempts("too many wrong attempts; wait a minute")
    rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings WHERE key IN ('always_lock_hash', 'always_lock_salt')")}
    if not rows.get("always_lock_hash"):
        return True
    ok = _hmac.compare_digest(_hash(password or "", bytes.fromhex(rows["always_lock_salt"])), rows["always_lock_hash"])
    if not ok:
        _fail_times.append(now)
        time.sleep(0.5)
    return ok


def require_password(conn, password):
    """Raise PermissionError unless the lock is unset or the password matches."""
    if not lock_is_set(conn):
        return
    if not password:
        raise PermissionError("the always-blocked list is locked: password required")
    if not verify_password(conn, password):
        raise PermissionError("wrong password")
