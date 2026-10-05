"""`tracker` command line: install/uninstall the background daemon, run the dashboard, export, backup."""
import argparse
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


def cmd_stop(args):
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
    with closing(db.connect()) as conn:
        db.set_setting(conn, args.key, args.value)
    print(f"{args.key} = {args.value}")


def cmd_get(args):
    with closing(db.connect()) as conn:
        s = db.get_settings(conn)
    if args.key:
        print(s.get(args.key, ""))
    else:
        for k in sorted(s):
            print(f"{k} = {s[k]}")


def cmd_pause(args):
    with closing(db.connect()) as conn:
        db.set_setting(conn, "paused", "0" if args.resume else "1")
    print("resumed" if args.resume else "paused (the daemon picks this up within 10s)")


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
    s = sub.add_parser("log", help="show the daemon log")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--dashboard", action="store_true")
    s.set_defaults(fn=cmd_log)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
