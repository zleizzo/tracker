# Tracker

A local, private replacement for RescueTime for macOS. A tiny native daemon records which app,
window and browser tab is in front of you (and when you are away); a local dashboard turns that into
projects, categories, sessions and trends. Nothing leaves the machine unless you point the hourly
backup at a cloud-synced folder.

```
daemon/     Swift menu-bar app (hourglass icon). Samples once a second, ~0% CPU, writes SQLite.
tracker/    Python (stdlib only): rules engine, analytics, JSON API, CLI.
web/        Dashboard: single page, no dependencies.
bin/tracker CLI entry point.
```

## Install

```sh
bin/tracker install
```

This compiles the daemon (works with just the Command Line Tools), copies it to
`~/Applications/TrackerDaemon.app`, installs two launch agents (daemon + dashboard server) that start
at login, and starts them. Then grant the two permissions macOS asks for:

1. **Accessibility** (System Settings › Privacy & Security › Accessibility › TrackerDaemon): needed
   for window titles, which is where VS Code's workspace name and browser tab titles come from.
2. **Automation**: the first time a browser is in front you get "TrackerDaemon wants to control
   Google Chrome". Allow it so the current tab URL is recorded. (If you decline, the daemon falls
   back to reading the address bar through Accessibility.)

Dashboard: <http://127.0.0.1:7898/> (also in the menu-bar menu). Data lives in
`~/Library/Application Support/Tracker/tracker.db`; logs in `~/Library/Logs/Tracker/`.

If you rebuild the daemon (`bin/tracker install` again), macOS sees a new app and you have to
re-tick it under Accessibility.

## Daily use

- **Overview**: tracked time, productive share, switches, context switching, time per day / hour,
  projects, apps, sites, tasks, top activities. The filter row (date range, project, category,
  hours of day, weekdays) scopes every chart and table; every chart has a table view.
- **Timeline**: one day as a track (category on top, project underneath). Click a block to assign
  that session to a project / task; click an idle gap to log what you did away from the laptop.
  Sessions are listed underneath with an Assign button, so a 10:00 VS Code block and an 11:00 one
  can go to different projects. Tick several sessions and use *Assign selected…* to attribute them
  all at once ("Select all unassigned" grabs everything still unsorted). "Log time away" is also
  in the menu-bar menu.
- **Remember for next time**: the assign dialog lists the specific windows, pages and editor
  workspaces that made up the session (a Zotero paper title, an Overleaf project URL, a VS Code
  repo…), with the ones that dominated the session pre-ticked. Leaving them ticked turns each into
  a rule, so the next time that paper or page is open it is attributed automatically; the rules
  show up on the Sort tab marked *auto* and can be edited or removed there like any other.
- **Plan**: set a plan for a day as time blocks, each tied to a project (optionally a task), with
  "copy previous day" / "copy last Tuesday" shortcuts. The page shows the planned blocks above what
  you actually worked on, and scores the day: adherence (planned time actually spent on the planned
  project), off-plan work during blocks, idle time during blocks, and work outside any block, per
  block and per project. For today the numbers are "so far"; once the day is over they are final.
  The Timeline shows the plan as an extra lane, the Overview gets an "On plan" tile, and Trends
  charts adherence per day.
- **Projects**: create projects (title, description, default category, estimated hours to
  completion) and sub-tasks (each with its own estimate); see time per project and per task for
  the selected range plus all-time progress against the estimate (percent done, active days,
  projected days left at your recent pace). Mark a task or project complete and the card records
  estimated vs actual ("estimated 20h, took 26h 30m (+32%) over 12 active days"). An "Estimation
  accuracy" card summarises all completed items: on average you take N× your estimate.
- **Sort**: an inbox of apps, sites and editor workspaces that have time but no rule yet. Pick a
  project (optionally a task) and/or a category and save; the rule applies to past and future
  time. Rules match the browser domain or URL, window title, editor workspace, bundle id or app
  name, case-insensitively (regex optional). Example: `domain youtube.com → distracting`, then
  `title "Attention Is All You Need" → Thesis, productive` with a higher priority.
- **Trends**: per-day stacks by project and category, weekday × hour heatmap, by hour, by weekday,
  switches per day/hour. Combine with the filter row for questions like "how productive am I on
  Tuesday mornings on the thesis".
- **Focus**: a website and app blocker that works at the network layer. Three modes: *blacklist*
  (your own list of sites and apps), *distracting* (the blacklist plus every site or app your rules
  sort as distracting) and *whitelist* (only the sites on your allow list can be reached). Pick a
  duration, optionally lock the session so it cannot be stopped early, and start. Also from the
  terminal: `bin/tracker focus start --mode distracting --minutes 45 --lock`.
- **Settings**: idle threshold, context-switch detection parameters, backup folder, export CSV,
  daemon health (whether window titles and URLs are actually arriving).

## How the blocker works (and why it holds)

macOS Screen Time filters inside the browser and is easy to slip past. Tracker instead sets a
proxy auto-config (PAC) on every network service for the length of the session (an admin user can
do this without sudo). Browsers and most apps evaluate the PAC for every single request, so it
does not matter what is cached, whether the window is incognito, whether Secure DNS is on, or
whether you type the URL by hand: hosts on the block list (and all their subdomains) are routed to
a local proxy on 127.0.0.1 that refuses the connection, and everything else goes direct. The
dashboard re-applies the setting every few seconds while the session runs, so turning it off in
System Settings only lasts until the next check, and restores the previous configuration when the
session ends. Blocked apps are quit every few seconds. Escape hatches, deliberately in the
terminal only: `bin/tracker focus stop --force` and `bin/tracker focus clear-proxy`.

Caveats: a tab that is already showing a blocked page keeps it until it loads something new;
whitelist mode limits every app that honours the system proxy, so add the domains your chat,
music or mail apps need; command-line tools that ignore proxy settings are not affected.

## Plans and estimates

- A plan block counts as followed for every second inside it that is attributed (by rules,
  assignments or logged time) to the block's project. Context switching never counts.
- "Outside the plan" is work that happened in no block at all; "off plan" is work on a different
  project during a block.
- All-time project and task totals are computed from the full history once per server process and
  cached per day, so the Projects tab stays fast even with a year of data (the first load after a
  restart can take a few seconds).

## How time is labelled

1. **Logged time** (manual entries for time away) replaces whatever was recorded in that range.
2. **Manual assignments** override rules for a time range, optionally only for one app.
3. **Rules**: highest priority wins, then the most specific field
   (url › title › workspace › domain › bundle › app). Rules remembered from an assignment get
   priority 1 so a specific page beats a general domain rule such as `youtube.com → distracting`.
4. **Project default category** when a rule sets a project but no category; else neutral.
5. **Context switching**: a run of ≥ 4 consecutive windows each held ≤ 10 s across ≥ 3 distinct
   windows is labelled "context switching" and never attributed to a project (tunable in Settings).

Idle: no input for 120 s (tunable), screen lock, sleep or screens off. The active segment is
trimmed back to the last input, so a walk away never inflates the last app.

## CLI

```
bin/tracker status | today [DATE] | dashboard | pause | resume | log [-f]
bin/tracker export --from 2026-09-01 --to 2026-09-30 --out sept.csv
curl 'http://127.0.0.1:7898/api/plan?date=2026-10-03'            # the whole JSON API is plain GET/POST
bin/tracker backup "~/Library/Mobile Documents/com~apple~CloudDocs/Tracker"   # sets the folder and copies now
bin/tracker focus add block youtube.com | focus add app Discord | focus status | focus stop
bin/tracker set idle_threshold_seconds 180
bin/tracker start | stop | restart | uninstall
```

## Cloud copy

Set a backup folder (Settings or `tracker backup DIR`). Every hour the daemon writes a consistent
snapshot `tracker-backup.db` there with `VACUUM INTO`; an iCloud Drive or Google Drive folder
works. The live database stays local so a sync client never touches an open SQLite file.
Add the `tracker/` package to a Python path on another machine and point `TRACKER_DB` at the copy to
browse it.

## Performance

The daemon is event-driven (app activations) plus a 1 Hz timer with 0.3 s tolerance; each tick is a
couple of Accessibility calls bounded to 0.5 s each. Browser URLs are only fetched when the tab
title changes (and at most every 15 s otherwise), via AppleScript on a background queue. Segment end
times are written every 5 s, so a crash loses at most 5 s. A busy day is ~2–3 000 rows; a year is a
few tens of MB.

## Development

```sh
bin/tracker build                      # compile daemon to daemon/build/
TRACKER_DB=/tmp/demo.db ~/anaconda3/bin/python3 -m tracker.demo   # seed fake data
TRACKER_DB=/tmp/demo.db bin/tracker serve --port 7899             # dashboard on fake data
```

Environment: `TRACKER_DB` (database path), `TRACKER_DATA_DIR`, `TRACKER_PYTHON` (interpreter for the
CLI; the `/usr/bin/python3` shim is skipped automatically when the Xcode license is unaccepted).

## License

MIT, see [LICENSE](LICENSE).
