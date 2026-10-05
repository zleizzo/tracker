"""Turns raw segments into labelled activity.

Resolution order for a segment's project/category:
  manual entry (time you logged yourself) > manual assignment (time range you relabelled)
  > matching rule (highest priority, then most specific field) > project default > neutral.
Runs of very short segments across several windows are labelled "context switching" and
never attributed to a project.
"""
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

BROWSERS = {
    "com.apple.Safari": "Safari", "com.apple.SafariTechnologyPreview": "Safari Technology Preview",
    "com.google.Chrome": "Google Chrome", "com.google.Chrome.canary": "Google Chrome Canary",
    "com.google.Chrome.beta": "Google Chrome Beta", "com.google.Chrome.dev": "Google Chrome Dev",
    "com.microsoft.edgemac": "Microsoft Edge", "com.microsoft.edgemac.Beta": "Microsoft Edge Beta",
    "com.microsoft.edgemac.Dev": "Microsoft Edge Dev",
    "com.brave.Browser": "Brave Browser", "com.brave.Browser.beta": "Brave Browser Beta",
    "com.brave.Browser.nightly": "Brave Browser Nightly",
    "com.vivaldi.Vivaldi": "Vivaldi", "com.operasoftware.Opera": "Opera", "com.operasoftware.OperaGX": "Opera GX",
    "company.thebrowser.Browser": "Arc", "company.thebrowser.dia": "Dia",
    "org.mozilla.firefox": "Mozilla Firefox", "org.mozilla.firefoxdeveloperedition": "Firefox Developer Edition",
    "org.mozilla.nightly": "Firefox Nightly", "app.zen-browser.zen": "Zen", "ai.perplexity.comet": "Comet",
    "com.kagi.kagimacOS": "Orion", "org.chromium.Chromium": "Chromium", "com.sigmaos.sigmaos": "SigmaOS",
}

EDITORS = {
    "com.microsoft.VSCode": "Visual Studio Code",
    "com.microsoft.VSCodeInsiders": "Visual Studio Code - Insiders",
    "com.vscodium": "VSCodium",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "com.exafunction.windsurf": "Windsurf",
    "dev.zed.Zed": "Zed",
    "com.apple.dt.Xcode": "Xcode",
    "com.sublimetext.4": "Sublime Text",
    "com.jetbrains.pycharm": "PyCharm", "com.jetbrains.intellij": "IntelliJ IDEA",
    "com.jetbrains.WebStorm": "WebStorm", "com.jetbrains.goland": "GoLand",
    "com.jetbrains.CLion": "CLion", "com.jetbrains.rider": "Rider",
}

CATEGORIES = ("productive", "neutral", "distracting")
SWITCHING = "switching"

# Rule fields, most specific first. Specificity breaks ties between rules with equal priority.
FIELD_SPECIFICITY = {"url": 6, "title": 5, "workspace": 4, "domain": 3, "bundle": 2, "app": 1}

_SEP_RE = re.compile(r"\s+[—–-]\s+")
_SUFFIX_RE = re.compile(r"\s+[—–-]\s+([^—–-]+)$")
_EDITOR_MARKERS = re.compile(r"\s*\[(?:Unsupported|Extension Development Host|Administrator|Superuser)\]\s*")
_FILE_LIKE = re.compile(r"^[^\s/]+\.[A-Za-z0-9]{1,10}$")
_NOT_WORKSPACE = re.compile(r"^(Welcome|Settings|Extensions?|Keyboard Shortcuts|Untitled-\d+|Release Notes.*|Search|Explorer|.* window)$", re.I)


def _strip_app_suffix(title, names):
    for name in sorted(names, key=len, reverse=True):
        for sep in (" — ", " - ", " – "):
            suffix = sep + name
            if title.endswith(suffix):
                return title[: -len(suffix)]
    return title


def clean_title(bundle, title):
    """Window title without the trailing app name browsers and editors append."""
    if not title:
        return ""
    t = title.strip()
    if bundle in BROWSERS:
        t = _strip_app_suffix(t, set(BROWSERS.values()) | {"Google Chrome", "Chrome", "Brave", "Firefox"})
        # Chrome with multiple profiles: "Page - Google Chrome - Zach"
        m = re.search(r"\s+[—–-]\s+Google Chrome\s+[—–-]\s+.+$", t)
        if m:
            t = t[: m.start()]
    elif bundle in EDITORS:
        t = _EDITOR_MARKERS.sub(" ", t).strip()
        t = _strip_app_suffix(t, set(EDITORS.values()))
        parts = [p.strip() for p in _SEP_RE.split(t) if p.strip()]
        if len(parts) >= 2:  # "file — workspace": the workspace is reported separately
            t = " — ".join(parts[:-1])
    return t.strip()


def workspace_from_title(bundle, title):
    """Best-effort repo / workspace name for editors (VS Code: 'file — workspace — Visual Studio Code')."""
    if bundle not in EDITORS or not title:
        return None
    t = _EDITOR_MARKERS.sub(" ", title).strip()
    t = _strip_app_suffix(t, set(EDITORS.values()))
    parts = [p.strip() for p in _SEP_RE.split(t) if p.strip()]
    if not parts:
        return None
    ws = parts[-1]
    ws = re.sub(r"\s*\((Workspace|Repository)\)$", "", ws).strip()
    ws = ws.lstrip("● ").strip()
    if len(parts) == 1 and (_FILE_LIKE.match(ws) or _NOT_WORKSPACE.match(ws)):
        return None  # a lone file name or an editor page, no folder open
    return ws or None


def domain_of(url):
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else "https://" + url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def enrich(seg):
    """Add derived fields used by rules and reports."""
    bundle = seg.get("bundle") or ""
    seg["title_clean"] = clean_title(bundle, seg.get("title"))
    seg["domain"] = seg.get("domain") or domain_of(seg.get("url"))
    seg["workspace"] = workspace_from_title(bundle, seg.get("title"))
    seg["context"] = seg["domain"] or seg["workspace"] or ""
    return seg


class Rule:
    __slots__ = ("id", "field", "pattern", "is_regex", "project_id", "task_id", "category", "priority", "_rx", "_needle")

    def __init__(self, row):
        self.id = row["id"]
        self.field = row["field"]
        self.pattern = row["pattern"]
        self.is_regex = bool(row["is_regex"])
        self.project_id = row["project_id"]
        self.task_id = row["task_id"]
        self.category = row["category"]
        self.priority = row["priority"] or 0
        self._rx = None
        self._needle = None
        if self.is_regex:
            try:
                self._rx = re.compile(self.pattern, re.I)
            except re.error:
                self._rx = re.compile(re.escape(self.pattern), re.I)
        else:
            self._needle = self.pattern.lower()

    def matches(self, seg):
        if self.field == "app":
            value = seg.get("app") or ""
        elif self.field == "bundle":
            value = seg.get("bundle") or ""
        elif self.field == "domain":
            value = seg.get("domain") or ""
        elif self.field == "url":
            value = seg.get("url") or ""
        elif self.field == "title":
            value = seg.get("title_clean") or seg.get("title") or ""
        elif self.field == "workspace":
            value = seg.get("workspace") or ""
        else:
            return False
        if not value:
            return False
        if self._rx is not None:
            return bool(self._rx.search(value))
        if self.field == "domain":
            v = value.lower()
            return v == self._needle or v.endswith("." + self._needle) or self._needle in v
        return self._needle in value.lower()


class Classifier:
    def __init__(self, conn, settings):
        self.settings = settings
        self.projects = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM projects")}
        self.tasks = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
        rules = [Rule(dict(r)) for r in conn.execute("SELECT * FROM rules")]
        rules.sort(key=lambda r: (-r.priority, -FIELD_SPECIFICITY.get(r.field, 0), -r.id))
        self.rules = rules
        self.assignments = [dict(r) for r in conn.execute("SELECT * FROM assignments ORDER BY created_at, id")]
        self._cache = {}

    # -- per-segment resolution -------------------------------------------------

    def _rule_for(self, seg):
        key = (seg.get("bundle"), seg.get("title"), seg.get("url"))
        if key in self._cache:
            return self._cache[key]
        hit = None
        for r in self.rules:
            if r.matches(seg):
                hit = r
                break
        self._cache[key] = hit
        return hit

    def _assignment_for(self, seg):
        mid = (seg["start"] + seg["end"]) / 2
        hit = None
        for a in self.assignments:  # latest created wins, so keep scanning
            if a["start"] <= mid < a["end"] and (not a["bundle"] or a["bundle"] == seg.get("bundle")):
                hit = a
        return hit

    def _finish(self, seg, project_id, task_id, category, source):
        if task_id and task_id in self.tasks and not project_id:
            project_id = self.tasks[task_id]["project_id"]
        if project_id and project_id not in self.projects:
            project_id = None
        if task_id and (task_id not in self.tasks or self.tasks[task_id]["project_id"] != project_id):
            task_id = None
        if category not in CATEGORIES:
            category = self.projects[project_id]["category"] if project_id else "neutral"
        seg["project_id"] = project_id
        seg["task_id"] = task_id
        seg["category"] = category
        seg["source"] = source
        seg["cs"] = False
        return seg

    def label(self, seg):
        if seg.get("kind") == "manual":
            return self._finish(seg, seg.get("project_id"), seg.get("task_id"), seg.get("category"), "manual")
        if seg.get("kind") != "active":
            seg.update(project_id=None, task_id=None, category=None, source="idle", cs=False)
            return seg
        a = self._assignment_for(seg)
        if a:
            return self._finish(seg, a["project_id"], a["task_id"], a["category"], "assignment")
        r = self._rule_for(seg)
        if r:
            return self._finish(seg, r.project_id, r.task_id, r.category, "rule")
        return self._finish(seg, None, None, None, "default")

    # -- whole-stream passes ----------------------------------------------------

    def mark_context_switching(self, segs):
        max_sec = float(self.settings.get("cs_max_seconds", 10))
        min_count = int(self.settings.get("cs_min_count", 4))
        min_distinct = int(self.settings.get("cs_min_distinct", 3))
        active = [s for s in segs if s.get("kind") == "active"]
        i, n = 0, len(active)
        while i < n:
            if active[i]["end"] - active[i]["start"] <= max_sec:
                j = i
                while (
                    j < n
                    and active[j]["end"] - active[j]["start"] <= max_sec
                    and (j == i or active[j]["start"] - active[j - 1]["end"] < 2)
                ):
                    j += 1
                run = active[i:j]
                distinct = {(s.get("bundle"), s.get("title"), s.get("url")) for s in run}
                if len(run) >= min_count and len(distinct) >= min_distinct:
                    for s in run:
                        s["cs"] = True
                        s["project_id"] = None
                        s["task_id"] = None
                        s["category"] = SWITCHING
                        s["source"] = "context_switching"
                i = j
            else:
                i += 1
        return segs

    def classify(self, segs):
        for s in segs:
            enrich(s)
            self.label(s)
        return self.mark_context_switching(segs)


def apply_manual_entries(segs, entries):
    """Manual entries replace whatever was tracked during their time range and become segments of kind 'manual'."""
    if not entries:
        return segs
    out = []
    for s in segs:
        pieces = [(s["start"], s["end"])]
        for e in entries:
            if e["end"] <= s["start"] or e["start"] >= s["end"]:
                continue
            nxt = []
            for a, b in pieces:
                if e["end"] <= a or e["start"] >= b:
                    nxt.append((a, b))
                    continue
                if a < e["start"]:
                    nxt.append((a, e["start"]))
                if e["end"] < b:
                    nxt.append((e["end"], b))
            pieces = nxt
        for a, b in pieces:
            if b - a > 0:
                piece = dict(s)
                piece["start"], piece["end"] = a, b
                out.append(piece)
    for e in entries:
        out.append({
            "id": -int(e["id"]), "start": e["start"], "end": e["end"], "kind": "manual",
            "app": e["title"], "bundle": "manual", "title": e["title"], "url": None, "domain": None,
            "switch_kind": "manual", "manual_id": e["id"], "project_id": e.get("project_id"),
            "task_id": e.get("task_id"), "category": e.get("category"), "note": e.get("note") or "",
        })
    out.sort(key=lambda s: s["start"])
    return out


_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|igshid|si|feature)$", re.I)


def canonical_url(url):
    """A stable, scheme-less form of a URL that identifies the page: no fragment, no tracking params."""
    if not url:
        return None
    from urllib.parse import parse_qsl, urlencode
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/")
    out = host + path
    if query:
        out += "?" + urlencode(query)
    return out


def identity_for(seg):
    """The thing that identifies 'the same window' for auto-rules: a page URL for browsers, the editor
    workspace for editors, otherwise the cleaned window title. Returns (field, pattern, label) or None."""
    bundle = seg.get("bundle") or ""
    app = seg.get("app") or ""
    title = seg.get("title_clean") or clean_title(bundle, seg.get("title"))
    if bundle in BROWSERS:
        cu = canonical_url(seg.get("url"))
        if cu and "/" in cu or (cu and "?" in cu):
            return ("url", cu, title or cu)
        if cu:
            return ("domain", cu, title or cu)
        return None
    ws = seg.get("workspace") or workspace_from_title(bundle, seg.get("title"))
    if ws:
        return ("workspace", ws, f"{ws} ({app})")
    if not title or title.lower() == app.lower() or len(title) < 3:
        return None
    return ("title", title, f"{title} ({app})")


def build_sessions(segs, gap_seconds=120):
    """Contiguous-ish stretches in one app. Short glances at other apps don't break a session;
    idle, locked and manual time do. Useful for 'assign this 10am VS Code block to project X'."""
    sessions = []
    last_by_bundle = {}
    last_break = -1.0
    for s in segs:
        if s.get("kind") != "active":
            last_break = max(last_break, s["end"])
            continue
        b = s.get("bundle") or s.get("app") or "?"
        sess = last_by_bundle.get(b)
        dur = s["end"] - s["start"]
        if sess and s["start"] - sess["end"] <= gap_seconds and sess["end"] >= last_break:
            sess["end"] = max(sess["end"], s["end"])
            sess["seconds"] += dur
            sess["count"] += 1
        else:
            sess = {
                "id": f"{b}:{int(s['start'])}", "bundle": b, "app": s.get("app") or b,
                "start": s["start"], "end": s["end"], "seconds": dur, "count": 1,
                "contexts": {}, "projects": {}, "categories": {}, "titles": {}, "identities": {},
            }
            sessions.append(sess)
            last_by_bundle[b] = sess
        ctx = s.get("context") or ""
        if ctx:
            sess["contexts"][ctx] = sess["contexts"].get(ctx, 0) + dur
        pid = "cs" if s.get("cs") else (s.get("project_id") or "none")
        sess["projects"][str(pid)] = sess["projects"].get(str(pid), 0) + dur
        cat = s.get("category") or "neutral"
        sess["categories"][cat] = sess["categories"].get(cat, 0) + dur
        tc = s.get("title_clean") or ""
        if tc:
            sess["titles"][tc] = sess["titles"].get(tc, 0) + dur
        ident = identity_for(s)
        if ident:
            key = f"{ident[0]}:{ident[1].lower()}"
            entry = sess["identities"].get(key)
            if entry is None:
                entry = sess["identities"][key] = {"field": ident[0], "pattern": ident[1], "label": ident[2], "seconds": 0.0, "project_id": s.get("project_id"), "source": s.get("source")}
            entry["seconds"] += dur
    for sess in sessions:
        sess["context"] = max(sess["contexts"], key=sess["contexts"].get) if sess["contexts"] else ""
        sess["title"] = max(sess["titles"], key=sess["titles"].get) if sess["titles"] else ""
        sess["contexts"] = dict(sorted(sess["contexts"].items(), key=lambda kv: -kv[1])[:6])
        sess["identities"] = sorted(sess["identities"].values(), key=lambda e: -e["seconds"])[:12]
        del sess["titles"]
    sessions.sort(key=lambda s: s["start"])
    return sessions


def split_hourly(start, end):
    """Yield (start, end, hour, weekday, day) pieces of [start, end) cut at local hour boundaries."""
    t = start
    while t < end:
        dt = datetime.fromtimestamp(t)
        boundary = (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
        e = min(end, boundary)
        if e <= t:  # DST oddities; never loop forever
            e = end
        yield t, e, dt.hour, dt.weekday(), dt.strftime("%Y-%m-%d")
        t = e
