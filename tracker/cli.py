"""`tracker` command line: install/uninstall the background daemon, run the dashboard, export, backup."""
import argparse
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta

from . import __version__, db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
APP_SRC = os.path.join(ROOT, "daemon", "build", "TrackerDaemon.app")
APP_INSTALL = os.path.join(HOME, "Applications", "TrackerDaemon.app")
AGENTS_DIR = os.path.join(HOME, "Library", "LaunchAgents")
LOG_DIR = os.path.join(HOME, "Library", "Logs", "Tracker")
DAEMON_LABEL = "com.tracker.daemon"
DASH_LABEL = "com.tracker.dashboard"
TRACKER_BIN = os.path.join(ROOT, "bin", "tracker")


def sh(args, check=False, quiet=True):
    return subprocess.run(args, capture_output=quiet, text=True, check=check)


def gui_domain():
    return f"gui/{os.getuid()}"


def plist_path(label):
    return os.path.join(AGENTS_DIR, label + ".plist")


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def env_passthrough():
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    for k in ("TRACKER_DB", "TRACKER_DATA_DIR", "TRACKER_PYTHON"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# -- build / install -----------------------------------------------------------

def cmd_build(args):
    r = subprocess.run(["/bin/bash", os.path.join(ROOT, "daemon", "build.sh")])
    if r.returncode != 0:
        sys.exit("daemon build failed")


def write_agents():
    os.makedirs(AGENTS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    daemon = {
        "Label": DAEMON_LABEL,
        "ProgramArguments": [os.path.join(APP_INSTALL, "Contents", "MacOS", "TrackerDaemon")],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": os.path.join(LOG_DIR, "daemon.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, "daemon.log"),
        "EnvironmentVariables": env_passthrough(),
    }
    dash = {
        "Label": DASH_LABEL,
        "ProgramArguments": [TRACKER_BIN, "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "WorkingDirectory": ROOT,
        "StandardOutPath": os.path.join(LOG_DIR, "dashboard.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, "dashboard.log"),
        "EnvironmentVariables": env_passthrough(),
    }
    for label, content in ((DAEMON_LABEL, daemon), (DASH_LABEL, dash)):
        with open(plist_path(label), "wb") as fh:
            plistlib.dump(content, fh)


def bootout(label):
    sh(["launchctl", "bootout", f"{gui_domain()}/{label}"])


def bootstrap(label):
    r = sh(["launchctl", "bootstrap", gui_domain(), plist_path(label)])
    if r.returncode != 0 and "already" not in (r.stderr or "").lower():
        print(f"warning: launchctl bootstrap {label}: {r.stderr.strip() or r.stdout.strip()}")
    sh(["launchctl", "kickstart", "-k", f"{gui_domain()}/{label}"])


def cmd_install(args):
    if not args.no_build:
        cmd_build(args)
    if not os.path.isdir(APP_SRC):
        sys.exit(f"missing {APP_SRC}; run `tracker build` first")
    bootout(DAEMON_LABEL)
    bootout(DASH_LABEL)
    os.makedirs(os.path.dirname(APP_INSTALL), exist_ok=True)
    if os.path.isdir(APP_INSTALL):
        shutil.rmtree(APP_INSTALL)
    shutil.copytree(APP_SRC, APP_INSTALL, symlinks=True)
    write_agents()
    with closing(db.connect()) as conn:  # make sure the database and defaults exist before anything starts
        port = db.get_settings(conn).get("dashboard_port", "7898")
    bootstrap(DAEMON_LABEL)
    bootstrap(DASH_LABEL)
    time.sleep(1.5)
    print(f"""
Tracker installed.
  daemon:     {APP_INSTALL}  (launchd {DAEMON_LABEL}, starts at login, menu-bar hourglass)
  dashboard:  http://127.0.0.1:{port}/   (launchd {DASH_LABEL})
  database:   {db.db_path()}
  logs:       {LOG_DIR}

Permissions (one-time, macOS will prompt):
  1. Accessibility  -> window titles, VS Code workspaces, tab switches, URL fallback.
     System Settings > Privacy & Security > Accessibility > enable TrackerDaemon.
  2. Automation     -> browser tab URLs. Approve "TrackerDaemon wants to control Google Chrome/Safari/..."
     when the prompt appears (it appears the first time a browser is in front).
If you rebuild the daemon later, macOS treats it as a new app: re-enable it under Accessibility.
""")
    if not args.no_open:
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
    cmd_status(args)


def cmd_uninstall(args):
    with closing(db.connect()) as conn:
        _require_lock_password(conn, None, "uninstalling (lifts always-blocking)")
    _clear_focus_proxy()
    bootout(DAEMON_LABEL)
    bootout(DASH_LABEL)
    for label in (DAEMON_LABEL, DASH_LABEL):
        try:
            os.remove(plist_path(label))
        except FileNotFoundError:
            pass
    if os.path.isdir(APP_INSTALL):
        shutil.rmtree(APP_INSTALL)
    print(f"uninstalled. Your data is untouched at {db.db_path()}")


def cmd_start(args):
    for label in (DAEMON_LABEL, DASH_LABEL):
        if os.path.exists(plist_path(label)):
            bootstrap(label)
    cmd_status(args)


def _clear_focus_proxy():
    try:
        from . import focus
        with closing(db.connect()) as conn:
            port = int(db.get_settings(conn).get("dashboard_port") or 7898)
        changed = focus.clear_pac(focus.Enforcer(port).is_ours)
        if changed:
            print(f"cleared focus proxy config on: {', '.join(changed)}")
    except Exception as e:  # noqa: BLE001
        print(f"warning: could not clear focus proxy config: {e}")


def cmd_stop(args):
    with closing(db.connect()) as conn:
        _require_lock_password(conn, None, "stopping the tracker (lifts always-blocking)")
    _clear_focus_proxy()
    bootout(DAEMON_LABEL)
    bootout(DASH_LABEL)
    print("stopped daemon and dashboard (they will start again at login; `tracker uninstall` removes them)")


def cmd_restart(args):
    for label in (DAEMON_LABEL, DASH_LABEL):
        sh(["launchctl", "kickstart", "-k", f"{gui_domain()}/{label}"])
    time.sleep(1)
    cmd_status(args)


# -- status / info -------------------------------------------------------------

def pid_of(name):
    r = sh(["pgrep", "-x", name])
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_status(args):
    with closing(db.connect()) as conn:
        settings = db.get_settings(conn)
        last = db.one(conn, "SELECT * FROM segments ORDER BY id DESC LIMIT 1")
        n = db.one(conn, "SELECT count(*) c, min(start) s FROM segments")
    port = int(settings.get("dashboard_port") or 7898)
    dpid = pid_of("TrackerDaemon")
    print(f"tracker {__version__}")
    print(f"  daemon:    {'running (pid ' + dpid + ')' if dpid else 'NOT running'}{'  [paused]' if settings.get('paused') == '1' else ''}")
    print(f"  dashboard: {'http://127.0.0.1:%d/' % port if port_open(port) else 'not listening on port %d' % port}")
    path = db.db_path()
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"  database:  {path} ({size / 1e6:.1f} MB, {n['c']} segments" +
          (f", since {datetime.fromtimestamp(n['s']):%Y-%m-%d}" if n["s"] else "") + ")")
    if last:
        age = time.time() - last["end"]
        what = last["app"] or last["kind"]
        if last["domain"]:
            what += f" · {last['domain']}"
        print(f"  last seen: {what} ({last['kind']}), {int(age)}s ago" + ("" if last["title"] or last["kind"] != "active" else "  [no window title: grant Accessibility]"))
    if settings.get("backup_dir"):
        lb = settings.get("last_backup")
        print(f"  backup:    {settings['backup_dir']}" + (f" (last {datetime.fromtimestamp(float(lb)):%Y-%m-%d %H:%M})" if lb else " (not yet run)"))


def cmd_today(args):
    from . import analytics
    day = args.date or datetime.now().strftime("%Y-%m-%d")
    with closing(db.connect()) as conn:
        t0, t1 = analytics.day_bounds(day)
        s = analytics.summary(conn, t0, t1, analytics.Filters())
    t = s["totals"]
    print(f"{day}: tracked {fmt_dur(t['active'])}  productive {fmt_dur(t['productive'])}  neutral {fmt_dur(t['neutral'])}  "
          f"distracting {fmt_dur(t['distracting'])}  switching {fmt_dur(t['switching'])}  idle {fmt_dur(t['idle'])}  "
          f"switches {t['switches']['total']}" + (f"  pulse {t['pulse']}%" if t["pulse"] is not None else ""))
    if s["by_project"]:
        print("  projects:")
        for p in s["by_project"][:10]:
            print(f"    {fmt_dur(p['seconds']):>8}  {p['title']}")
    if s["by_app"]:
        print("  apps:")
        for a in s["by_app"][:10]:
            print(f"    {fmt_dur(a['seconds']):>8}  {a['app']}")


# -- dashboard / data ----------------------------------------------------------

def cmd_serve(args):
    from .server import serve
    serve(args.port)


def cmd_dashboard(args):
    with closing(db.connect()) as conn:
        port = args.port or int(db.get_settings(conn).get("dashboard_port") or 7898)
    url = f"http://127.0.0.1:{port}/"
    if not port_open(port):
        print(f"dashboard not running; starting it in the background on {url}")
        subprocess.Popen([TRACKER_BIN, "serve", "--port", str(port)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(30):
            if port_open(port):
                break
            time.sleep(0.2)
    if not args.no_open:
        subprocess.run(["open", url])
    print(url)


def cmd_export(args):
    from . import analytics
    frm = args.frm or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    to = args.to or datetime.now().strftime("%Y-%m-%d")
    with closing(db.connect()) as conn:
        t0, t1 = analytics.range_bounds(frm, to)
        data = analytics.export_csv(conn, t0, t1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(data)
        print(f"wrote {args.out} ({data.count(chr(10)) - 1} rows)")
    else:
        sys.stdout.write(data)


def cmd_backup(args):
    from .server import run_backup
    with closing(db.connect()) as conn:
        if args.dir:
            db.set_setting(conn, "backup_dir", args.dir)
        r = run_backup(conn, args.dir)
    print(f"backup written: {r['path']} ({r['bytes'] / 1e6:.1f} MB)")


def cmd_set(args):
    if args.key not in db.DEFAULT_SETTINGS:
        sys.exit(f"unknown setting '{args.key}'. Known: {', '.join(db.DEFAULT_SETTINGS)}")
    from . import focus
    if args.key in focus.PROTECTED_SETTINGS:
        sys.exit(f"'{args.key}' is managed by `tracker focus hours` (it may require the lock password)")
    with closing(db.connect()) as conn:
        db.set_setting(conn, args.key, args.value)
    print(f"{args.key} = {args.value}")


def cmd_get(args):
    from . import focus
    with closing(db.connect()) as conn:
        s = {k: v for k, v in db.get_settings(conn).items() if k not in focus.SECRET_SETTINGS}
    if args.key:
        print(s.get(args.key, ""))
    else:
        for k in sorted(s):
            print(f"{k} = {s[k]}")


def cmd_pause(args):
    with closing(db.connect()) as conn:
        db.set_setting(conn, "paused", "0" if args.resume else "1")
    print("resumed" if args.resume else "paused (the daemon picks this up within 10s)")


def _require_lock_password(conn, given=None, why="this"):
    """Ask for the always-list lock password (if one is set) before an operation that lifts blocking."""
    from . import focus
    if not focus.lock_is_set(conn):
        return
    import getpass
    pw = given if given is not None else getpass.getpass(f"Lock password required for {why}: ")
    try:
        if not focus.verify_password(conn, pw):
            sys.exit("wrong password")
    except focus.TooManyAttempts as e:
        sys.exit(str(e))


def cmd_focus(args):
    from . import focus
    sub = args.focus_cmd
    with closing(db.connect()) as conn:
        port = int(db.get_settings(conn).get("dashboard_port") or 7898)
        enforcer = focus.Enforcer(port)
        if sub == "start":
            try:
                s = focus.start_session(conn, args.mode, args.minutes, args.lock)
            except ValueError as e:
                sys.exit(f"error: {e}")
            enforcer.tick()
            st = enforcer.state
            print(f"focus ({s['mode']}) until {datetime.fromtimestamp(s['end']):%H:%M}: blocking {len(st['block'])} site(s), {len(st['apps'])} app(s)"
                  + (f"; allowing only {len(st['allow'])} site(s)" if s["mode"] == "whitelist" else "") + ("  [locked]" if s["locked"] else ""))
            if not port_open(port):
                print("warning: the dashboard is not running, so the proxy config will not be served or kept applied. Run `tracker start`.")
        elif sub == "stop":
            try:
                s = focus.stop_session(conn, force=args.force)
            except ValueError as e:
                sys.exit(f"error: {e} (use --force)")
            enforcer.tick()
            print("focus session stopped" if s else "no focus session was running")
        elif sub == "extend":
            try:
                s = focus.extend_session(conn, args.minutes)
            except ValueError as e:
                sys.exit(f"error: {e}")
            enforcer.tick()
            print(f"extended until {datetime.fromtimestamp(s['end']):%H:%M}")
        elif sub == "add":
            try:
                row = focus.add_site(conn, args.list, args.pattern)
            except ValueError as e:
                sys.exit(f"error: {e}")
            enforcer.tick()
            print(f"added {row['pattern']} to the {args.list} list")
        elif sub == "remove":
            if args.list in focus.ALWAYS_LISTS:
                _require_lock_password(conn, args.password, f"removing {args.pattern} from the always-blocked list")
            cur = conn.execute("DELETE FROM focus_sites WHERE list = ? AND lower(pattern) = lower(?)", (args.list, args.pattern.strip()))
            conn.commit()
            enforcer.tick()
            print("removed" if cur.rowcount else "not found")
        elif sub == "lock":
            import getpass
            if args.off:
                focus.clear_lock(conn, args.password if args.password is not None else getpass.getpass("Current lock password: "))
                print("lock removed")
                return
            current = None
            if focus.lock_is_set(conn):
                current = args.password if args.password is not None else getpass.getpass("Current lock password: ")
            new = getpass.getpass("New lock password: ")
            if new != getpass.getpass("Repeat: "):
                sys.exit("passwords do not match")
            try:
                focus.set_lock(conn, new, current)
            except (PermissionError, ValueError) as e:
                sys.exit(f"error: {e}")
            print("lock password set: removing always-blocked entries, `tracker stop`, `uninstall` and `focus clear-proxy` now require it")
        elif sub == "hours":
            windows = focus.parse_windows(db.get_settings(conn))
            if args.hours_cmd == "add":
                _require_lock_password(conn, args.password, "changing the unblocked hours")
                try:
                    start, end = args.range.split("-")
                    days = [d.strip() for d in args.days.split(",")] if args.days else None
                    windows = focus.normalize_windows(windows + [{"start": start, "end": end, "days": days}])
                except ValueError as e:
                    sys.exit(f"error: {e} (use HH:MM-HH:MM, e.g. 19:00-23:00)")
                db.set_setting(conn, "always_allow_windows", json.dumps(windows))
                enforcer.tick()
            elif args.hours_cmd == "remove":
                _require_lock_password(conn, args.password, "changing the unblocked hours")
                if not (1 <= args.index <= len(windows)):
                    sys.exit("no such window; `tracker focus hours` lists them with their numbers")
                windows.pop(args.index - 1)
                db.set_setting(conn, "always_allow_windows", json.dumps(windows))
                enforcer.tick()
            if not windows:
                print("always-blocked list is enforced 24/7 (no unblocked hours)")
            for i, w in enumerate(windows, 1):
                days = "every day" if len(w["days"]) == 7 else ", ".join(focus.DAY_NAMES[d].capitalize() for d in w["days"])
                print(f"  {i}. {w['start']}–{w['end']}  {days}")
            paused = focus.in_allowed_window(windows)
            nxt = focus.next_window_change(windows)
            print(f"  now: {'paused (unblocked)' if paused else 'blocking'}" + (f", changes at {datetime.fromtimestamp(nxt):%a %H:%M}" if nxt else ""))
        elif sub == "clear-proxy":
            _require_lock_password(conn, args.password, "clearing the proxy config")
            changed = focus.clear_pac(enforcer.is_ours)
            print(f"proxy config cleared on: {', '.join(changed) or 'nothing (none of ours was applied)'}")
        else:  # status / list
            focus.close_expired(conn)
            s = focus.active_session(conn)
            lists = focus.get_lists(conn)
            if s:
                left = max(0, s["end"] - time.time())
                print(f"focus: {s['mode']} until {datetime.fromtimestamp(s['end']):%H:%M} ({fmt_dur(left)} left){'  [locked]' if s['locked'] else ''}")
            else:
                print("focus: no session running")
            print(f"  applied on: {', '.join(focus.applied_services(enforcer.is_ours)) or 'none'}")
            st = focus.effective_state(conn)
            print(f"  always:     {', '.join(r['pattern'] for r in lists['always'] + lists['alwaysapp']) or '-'}"
                  + ("  [paused by unblocked hours" + (f" until {datetime.fromtimestamp(st['always_next_change']):%H:%M}" if st.get("always_next_change") else "") + "]" if st.get("always_paused") else ""))
            print(f"  blacklist:  {', '.join(r['pattern'] for r in lists['block']) or '-'}")
            print(f"  apps:       {', '.join(r['pattern'] for r in lists['app']) or '-'}")
            print(f"  whitelist:  {', '.join(r['pattern'] for r in lists['allow']) or '-'}")
            d = focus.distracting_from_rules(conn)
            print(f"  distracting (from rules): {', '.join(d['domains'] + d['apps']) or '-'}")


def cmd_reflections(args):
    from . import reflect
    with closing(db.connect()) as conn:
        if args.export:
            n = reflect.export_all(conn)
            print(f"wrote {n} file(s) to {reflect.export_dir()}")
            return
        if args.json:
            rows = reflect.list_entries(conn, args.since)
            for r in rows:
                r["stats"] = reflect.day_stats(conn, r["day"])
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        out = reflect.render_all(conn, args.since)
        print(out if out else f"no reflections yet (they live in {reflect.export_dir()})")


def cmd_reflect(args):
    from . import reflect
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    text = args.text
    if text is None:
        if sys.stdin.isatty():
            print(f"Reflection for {day}. Type your text, then Ctrl-D on an empty line:")
        text = sys.stdin.read()
    with closing(db.connect()) as conn:
        try:
            e = reflect.save(conn, day, text, args.rating)
        except ValueError as err:
            sys.exit(f"error: {err}")
    print(f"saved reflection for {e['day']} -> {reflect.export_dir()}/{e['day']}.md")


def cmd_notify(args):
    from . import notify
    ok = notify.send("Tracker", args.message or "Notifications are working.", "Test")
    print("notification posted" if ok else "could not post a notification")


def cmd_log(args):
    path = os.path.join(LOG_DIR, "dashboard.log" if args.dashboard else "daemon.log")
    if not os.path.exists(path):
        sys.exit(f"no log at {path}")
    subprocess.run(["tail", "-n", str(args.lines)] + (["-f"] if args.follow else []) + [path])


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracker", description="Local time tracker (RescueTime replacement)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("install", help="build the daemon, install it with launchd and start everything")
    s.add_argument("--no-build", action="store_true")
    s.add_argument("--no-open", action="store_true", help="don't open System Settings")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the launch agents and app (keeps data)").set_defaults(fn=cmd_uninstall)
    sub.add_parser("build", help="compile the Swift daemon").set_defaults(fn=cmd_build)
    sub.add_parser("start", help="start daemon + dashboard").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop daemon + dashboard until next login").set_defaults(fn=cmd_stop)
    sub.add_parser("restart", help="restart daemon + dashboard").set_defaults(fn=cmd_restart)
    sub.add_parser("status", help="what is running and what was last seen").set_defaults(fn=cmd_status)
    s = sub.add_parser("today", help="text summary for a day")
    s.add_argument("date", nargs="?")
    s.set_defaults(fn=cmd_today)
    s = sub.add_parser("serve", help="run the dashboard server in the foreground")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("dashboard", help="open the dashboard (starting the server if needed)")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("export", help="CSV of labelled segments")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("backup", help="copy the database to the backup folder (e.g. an iCloud/Drive folder)")
    s.add_argument("dir", nargs="?", help="set and use this folder")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("set", help="change a setting")
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser("get", help="show settings")
    s.add_argument("key", nargs="?")
    s.set_defaults(fn=cmd_get)
    s = sub.add_parser("pause", help="pause tracking")
    s.set_defaults(fn=cmd_pause, resume=False)
    s = sub.add_parser("resume", help="resume tracking")
    s.set_defaults(fn=cmd_pause, resume=True)
    s = sub.add_parser("focus", help="website / app blocking sessions")
    fs = s.add_subparsers(dest="focus_cmd")
    f = fs.add_parser("start", help="start a focus session")
    f.add_argument("--mode", choices=("blacklist", "distracting", "whitelist"), default="blacklist")
    f.add_argument("--minutes", type=float, default=25)
    f.add_argument("--lock", action="store_true", help="cannot be stopped early")
    f = fs.add_parser("stop", help="stop the running session")
    f.add_argument("--force", action="store_true", help="stop even if locked")
    f = fs.add_parser("extend", help="extend the running session")
    f.add_argument("minutes", type=float)
    f = fs.add_parser("add", help="add a site or app to a list (always/alwaysapp = blocked at all times)")
    f.add_argument("list", choices=("block", "allow", "app", "always", "alwaysapp"))
    f.add_argument("pattern")
    f = fs.add_parser("remove", help="remove a site or app from a list")
    f.add_argument("list", choices=("block", "allow", "app", "always", "alwaysapp"))
    f.add_argument("pattern")
    f.add_argument("--password", help="lock password (prompted if omitted)")
    f = fs.add_parser("lock", help="set or change the password that protects the always-blocked list")
    f.add_argument("--off", action="store_true", help="remove the lock")
    f.add_argument("--password", help="current password (prompted if omitted)")
    fs.add_parser("status", help="session, lists and where the proxy config is applied")
    f = fs.add_parser("hours", help="hours during which the always-blocked list is NOT enforced")
    hs = f.add_subparsers(dest="hours_cmd")
    h = hs.add_parser("add", help="add a window, e.g. 19:00-23:00 [--days mon,tue,wed]")
    h.add_argument("range")
    h.add_argument("--days", help="comma-separated weekdays (default every day)")
    h.add_argument("--password")
    h = hs.add_parser("remove", help="remove window number N")
    h.add_argument("index", type=int)
    h.add_argument("--password")
    f.set_defaults(hours_cmd="list")
    f = fs.add_parser("clear-proxy", help="emergency: remove our proxy config from all network services")
    f.add_argument("--password", help="lock password (prompted if omitted)")
    s.set_defaults(fn=cmd_focus, focus_cmd="status")
    s = sub.add_parser("reflections", help="print all daily reflections as Markdown (for review or analysis)")
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--json", action="store_true")
    s.add_argument("--export", action="store_true", help="(re)write the Markdown files and index")
    s.set_defaults(fn=cmd_reflections)
    s = sub.add_parser("reflect", help="write today's reflection (text from --text or stdin)")
    s.add_argument("day", nargs="?")
    s.add_argument("--text")
    s.add_argument("--rating", type=int)
    s.set_defaults(fn=cmd_reflect)
    s = sub.add_parser("notify", help="post a test notification")
    s.add_argument("message", nargs="?")
    s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("log", help="show the daemon log")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--dashboard", action="store_true")
    s.set_defaults(fn=cmd_log)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
