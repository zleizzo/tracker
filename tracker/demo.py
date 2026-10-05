"""Seed a scratch database with a plausible week of activity so the dashboard can be exercised.
Never run against the real database: `TRACKER_DB=/tmp/demo.db python -m tracker.demo`."""
import os
import random
import sys
import time
from datetime import datetime, timedelta

from . import db

APPS = {
    "code": ("Code", "com.microsoft.VSCode"),
    "chrome": ("Google Chrome", "com.google.Chrome"),
    "slack": ("Slack", "com.tinyspeck.slackmacgap"),
    "terminal": ("Terminal", "com.apple.Terminal"),
    "spotify": ("Spotify", "com.spotify.client"),
    "discord": ("Discord", "com.hnc.Discord"),
    "outlook": ("Microsoft Outlook", "com.microsoft.Outlook"),
    "preview": ("Preview", "com.apple.Preview"),
}
SITES = [
    ("github.com", "https://github.com/zach/tracker/pulls", "Pull requests · zach/tracker"),
    ("arxiv.org", "https://arxiv.org/abs/2403.01234", "[2403.01234] Scaling laws for time tracking"),
    ("youtube.com", "https://www.youtube.com/watch?v=abc123", "Attention Is All You Need explained - YouTube"),
    ("youtube.com", "https://www.youtube.com/watch?v=xyz789", "Top 10 cat fails compilation - YouTube"),
    ("news.ycombinator.com", "https://news.ycombinator.com/", "Hacker News"),
    ("reddit.com", "https://www.reddit.com/r/programming/", "r/programming"),
    ("docs.python.org", "https://docs.python.org/3/library/sqlite3.html", "sqlite3 — DB-API 2.0 interface"),
    ("twitter.com", "https://twitter.com/home", "Home / X"),
    ("stackoverflow.com", "https://stackoverflow.com/questions/1", "python - How do I read a window title - Stack Overflow"),
]
WORKSPACES = ["tracker", "thesis", "paper-figures"]
FILES = ["main.swift", "analytics.py", "app.js", "README.md", "train.py", "figures.ipynb"]


def seed(conn, days=10, seed=7):
    rnd = random.Random(seed)
    conn.execute("DELETE FROM segments")
    now = time.time()
    rows = []
    for d in range(days, -1, -1):
        day = (datetime.now() - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        if day.weekday() >= 5 and rnd.random() < 0.5:
            continue
        t = (day + timedelta(hours=rnd.uniform(8.5, 10))).timestamp()
        end_of_day = (day + timedelta(hours=rnd.uniform(17, 20))).timestamp()
        prev_bundle = None
        while t < end_of_day and t < now:
            r = rnd.random()
            if r < 0.06:  # break
                dur = rnd.uniform(5 * 60, 50 * 60)
                rows.append((t, t + dur, "idle", None, None, None, None, None, "idle"))
                t += dur
                prev_bundle = None
                continue
            if r < 0.12:  # context switching flurry
                for _ in range(rnd.randint(4, 8)):
                    name, bundle = rnd.choice(list(APPS.values()))
                    dur = rnd.uniform(1, 6)
                    rows.append((t, t + dur, "active", name, bundle, f"{name} window", None, None, "app"))
                    t += dur
                prev_bundle = None
                continue
            kind = rnd.choices(["code", "chrome", "slack", "terminal", "outlook", "preview", "spotify", "discord"],
                               weights=[30, 30, 10, 10, 6, 4, 3, 3])[0]
            name, bundle = APPS[kind]
            if kind == "code":
                ws = rnd.choice(WORKSPACES)
                title = f"{rnd.choice(FILES)} — {ws} — Visual Studio Code"
                url = domain = None
                dur = rnd.uniform(60, 25 * 60)
            elif kind == "chrome":
                domain, url, page = rnd.choice(SITES)
                title = f"{page} - Google Chrome"
                dur = rnd.uniform(20, 12 * 60)
            elif kind == "terminal":
                title, url, domain = "zach — -zsh — 120×40", None, None
                dur = rnd.uniform(20, 8 * 60)
            else:
                title, url, domain = f"{name}", None, None
                dur = rnd.uniform(15, 10 * 60)
            sk = "app" if bundle != prev_bundle else ("tab" if kind == "chrome" else "window")
            rows.append((t, t + dur, "active", name, bundle, title, url, domain, sk))
            t += dur
            prev_bundle = bundle
        rows.append((t, t + 3600 * 10, "locked", None, None, None, None, None, "idle"))
    conn.executemany("INSERT INTO segments(start,end,kind,app,bundle,title,url,domain,switch_kind) VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def project(title, desc, cat):
        cur = conn.execute("INSERT INTO projects(title, description, color, category, created_at) VALUES (?,?,?,?,?)",
                           (title, desc, conn.execute("SELECT count(*) FROM projects").fetchone()[0], cat, now))
        return cur.lastrowid

    if not conn.execute("SELECT 1 FROM projects").fetchone():
        p1 = project("Tracker", "Local RescueTime replacement", "productive")
        p2 = project("Thesis", "Chapter 3 experiments and writing", "productive")
        p3 = project("Admin", "Email, calendar, expenses", "neutral")
        t1 = conn.execute("INSERT INTO tasks(project_id, title, created_at) VALUES (?,?,?)", (p1, "Daemon", now)).lastrowid
        t2 = conn.execute("INSERT INTO tasks(project_id, title, created_at) VALUES (?,?,?)", (p1, "Dashboard", now)).lastrowid
        conn.execute("INSERT INTO tasks(project_id, title, created_at) VALUES (?,?,?)", (p2, "Figures", now))
        rules = [
            ("workspace", "tracker", 0, p1, None, None, 0),
            ("workspace", "thesis", 0, p2, None, None, 0),
            ("workspace", "paper-figures", 0, p2, None, None, 0),
            ("domain", "github.com", 0, p1, None, None, 0),
            ("domain", "arxiv.org", 0, p2, None, None, 0),
            ("title", "Attention Is All You Need", 0, p2, None, "productive", 5),
            ("domain", "youtube.com", 0, None, None, "distracting", 0),
            ("domain", "reddit.com", 0, None, None, "distracting", 0),
            ("domain", "twitter.com", 0, None, None, "distracting", 0),
            ("domain", "news.ycombinator.com", 0, None, None, "distracting", 0),
            ("domain", "docs.python.org", 0, p1, t2, None, 0),
            ("app", "Microsoft Outlook", 0, p3, None, None, 0),
            ("app", "Discord", 0, None, None, "distracting", 0),
            ("app", "Spotify", 0, None, None, "neutral", 0),
            ("title", "main.swift", 0, p1, t1, None, 1),
        ]
        conn.executemany("INSERT INTO rules(field, pattern, is_regex, project_id, task_id, category, priority, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         [r + (now,) for r in rules])
        yday = (datetime.now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        conn.execute("INSERT INTO manual_entries(start, end, title, project_id, category, note, created_at) VALUES (?,?,?,?,?,?,?)",
                     (yday, yday + 3600, "Whiteboard session with advisor", p2, "productive", "", now))
        # estimates and completion: Tracker is estimated at 30h; its Daemon task (6h) is finished; Admin is a finished 5h project
        conn.execute("UPDATE projects SET estimate_hours = 30 WHERE id = ?", (p1,))
        conn.execute("UPDATE projects SET estimate_hours = 60 WHERE id = ?", (p2,))
        conn.execute("UPDATE projects SET estimate_hours = 5, done = 1, completed_at = ? WHERE id = ?", (now - 86400, p3))
        conn.execute("UPDATE tasks SET estimate_hours = 6, done = 1, completed_at = ? WHERE id = ?", (now - 2 * 86400, t1))
        conn.execute("UPDATE tasks SET estimate_hours = 8 WHERE id = ?", (t2,))
        # a plan for yesterday and today
        for d in (0, 1, 2):
            day = (datetime.now() - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            base = day.timestamp()
            for (h0, h1, pid, tid) in ((9, 11, p2, None), (11, 12, p1, t2), (13, 15, p2, None), (15, 16.5, p1, None), (16.5, 17, p3, None)):
                conn.execute("INSERT INTO plan_blocks(day, start, end, project_id, task_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                             (day.strftime("%Y-%m-%d"), base + h0 * 3600, base + h1 * 3600, pid, tid, "", now))
    conn.commit()
    return len(rows)


if __name__ == "__main__":
    path = db.db_path()
    if "Application Support/Tracker" in path and "--force" not in sys.argv:
        sys.exit("refusing to seed the real database; set TRACKER_DB to a scratch file")
    with db.connect(path) as conn:
        n = seed(conn)
    print(f"seeded {n} segments into {path}")
