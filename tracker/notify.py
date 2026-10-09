"""macOS notifications: planned blocks about to start, and the end-of-day reflection reminder."""
import json
import subprocess
import threading
import time
from datetime import datetime

from . import db
from .analytics import day_bounds, plan_blocks


def send(title, message, subtitle=None, sound="Glass"):
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    if subtitle:
        script += f' subtitle "{esc(subtitle)}"'
    if sound:
        script += f' sound name "{esc(sound)}"'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


class Notifier:
    """Checked every 30 s by the dashboard. Keys of notifications already sent today are kept in the
    settings table so a restart does not repeat them."""

    def __init__(self, send_fn=send):
        self.send = send_fn
        self.last_error = None

    def _sent(self, conn):
        raw = db.get_settings(conn).get("notify_sent") or ""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("day") != today:
            data = {"day": today, "keys": []}
        return data

    def _mark(self, conn, data, key):
        data["keys"].append(key)
        db.set_setting(conn, "notify_sent", json.dumps(data))

    def check(self, now=None):
        now = now or time.time()
        fired = []
        with db.connect() as conn:
            settings = db.get_settings(conn)
            if settings.get("notifications", "1") != "1":
                return fired
            sent = self._sent(conn)
            today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            # planned blocks
            lead_raw = (settings.get("plan_notify_minutes") or "").strip()
            if lead_raw != "":
                lead = max(0, int(float(lead_raw)))
                for b in plan_blocks(conn, today):
                    key = f"block:{b['id']}:{int(b['start'])}"
                    fire_at = b["start"] - lead * 60
                    if key in sent["keys"] or not (fire_at <= now <= fire_at + 120):
                        continue
                    what = (b.get("project_title") or "Project") + (f" › {b['task_title']}" if b.get("task_title") else "")
                    when = datetime.fromtimestamp(b["start"]).strftime("%H:%M")
                    msg = f"starts now · until {datetime.fromtimestamp(b['end']).strftime('%H:%M')}" if lead == 0 else f"starts at {when} (in {lead} min) · until {datetime.fromtimestamp(b['end']).strftime('%H:%M')}"
                    if b.get("note"):
                        msg += f" · {b['note']}"
                    if self.send("Up next: " + what, msg, "Planned block"):
                        self._mark(conn, sent, key)
                        fired.append(key)
            # reflection reminder
            t = (settings.get("reflection_reminder_time") or "").strip()
            if t and ":" in t:
                try:
                    hh, mm = (int(x) for x in t.split(":"))
                    fire_at = day_bounds(today)[0] + hh * 3600 + mm * 60
                except ValueError:
                    fire_at = None
                key = f"reflect:{today}"
                if fire_at and key not in sent["keys"] and fire_at <= now <= fire_at + 900:
                    has = conn.execute("SELECT 1 FROM reflections WHERE day = ? AND text <> ''", (today,)).fetchone()
                    if not has:
                        port = settings.get("dashboard_port") or "7898"
                        if self.send("How did today go?", f"Write a few lines in the Journal · http://127.0.0.1:{port}/#journal", "Daily reflection"):
                            self._mark(conn, sent, key)
                            fired.append(key)
        return fired

    def loop(self):
        while True:
            try:
                self.check()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
            time.sleep(30)


def start_notifier():
    n = Notifier()
    threading.Thread(target=n.loop, name="notifier", daemon=True).start()
    return n
