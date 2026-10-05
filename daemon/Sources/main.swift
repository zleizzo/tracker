import AppKit
import ApplicationServices
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var store: Store!
    var tracker: Tracker!
    var statusItem: NSStatusItem!
    var infoItem: NSMenuItem!
    var permItem: NSMenuItem!
    var pauseItem: NSMenuItem!
    var tickTimer: Timer?
    var backupTimer: Timer?
    var activity: NSObjectProtocol?
    var sigterm: DispatchSourceSignal?
    var menuOpen = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        setvbuf(stdout, nil, _IOLBF, 0)
        do {
            store = try Store(path: Paths.dbPath)
        } catch {
            log("fatal: cannot open database: \(error)")
            exit(1)
        }
        log("tracker daemon starting (pid \(getpid())) db=\(Paths.dbPath)")
        tracker = Tracker(store: store)
        tracker.onChange = { [weak self] in self?.refreshMenu() }

        // Keep App Nap from throttling our 1 Hz timer; this does NOT keep the Mac awake.
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: "time tracking")

        setupStatusItem()
        setupNotifications()
        setupSignals()

        let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary)
        log("accessibility trusted: \(trusted)")
        if !trusted { log("window titles and tab URLs need Accessibility: System Settings > Privacy & Security > Accessibility > TrackerDaemon") }

        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.tracker.tick()
            if self.menuOpen { self.refreshMenu() }
        }
        tickTimer?.tolerance = 0.3
        backupTimer = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in self?.backup() }
        backupTimer?.tolerance = 60
        DispatchQueue.main.asyncAfter(deadline: .now() + 90) { [weak self] in self?.backup() }
        tracker.tick()
    }

    func applicationWillTerminate(_ notification: Notification) {
        tracker.flush()
        log("terminating")
    }

    // MARK: status item

    func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            if let img = NSImage(systemSymbolName: "hourglass", accessibilityDescription: "Tracker") {
                img.isTemplate = true
                button.image = img
            } else {
                button.title = "◔"
            }
        }
        let menu = NSMenu()
        menu.delegate = self
        infoItem = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
        infoItem.isEnabled = false
        menu.addItem(infoItem)
        permItem = NSMenuItem(title: "Grant Accessibility permission…", action: #selector(openAccessibility), keyEquivalent: "")
        permItem.target = self
        menu.addItem(permItem)
        menu.addItem(.separator())
        let dash = NSMenuItem(title: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "d")
        dash.target = self
        menu.addItem(dash)
        let logAway = NSMenuItem(title: "Log Time Away…", action: #selector(logTimeAway), keyEquivalent: "l")
        logAway.target = self
        menu.addItem(logAway)
        pauseItem = NSMenuItem(title: "Pause Tracking", action: #selector(togglePause), keyEquivalent: "p")
        pauseItem.target = self
        menu.addItem(pauseItem)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Tracker", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
        refreshMenu()
    }

    func menuWillOpen(_ menu: NSMenu) { menuOpen = true; refreshMenu() }
    func menuDidClose(_ menu: NSMenu) { menuOpen = false }

    func refreshMenu() {
        guard infoItem != nil else { return }
        if tracker.paused {
            infoItem.title = "Paused"
            pauseItem.title = "Resume Tracking"
        } else {
            pauseItem.title = "Pause Tracking"
            if let c = tracker.current {
                let mins = Int((Date().timeIntervalSince1970 - c.start) / 60)
                let dur = mins >= 60 ? "\(mins / 60)h \(mins % 60)m" : "\(mins)m"
                if c.kind == "active" {
                    var label = c.app ?? "?"
                    if let d = c.url.flatMap(Tracker.domain(of:)) { label += " · \(d)" }
                    infoItem.title = "Tracking \(label) · \(dur)"
                } else {
                    infoItem.title = c.kind == "locked" ? "Locked · \(dur)" : "Idle · \(dur)"
                }
            } else {
                infoItem.title = "Not tracking"
            }
        }
        permItem.isHidden = tracker.axTrusted
        statusItem.button?.appearsDisabled = tracker.paused
    }

    var dashboardURL: URL {
        let port = store.setting("dashboard_port") ?? "7898"
        return URL(string: "http://127.0.0.1:\(port)/")!
    }

    @objc func openDashboard() { NSWorkspace.shared.open(dashboardURL) }
    @objc func logTimeAway() { NSWorkspace.shared.open(URL(string: dashboardURL.absoluteString + "#timeline/log")!) }
    @objc func togglePause() { tracker.setPaused(!tracker.paused) }
    @objc func quit() { tracker.flush(); NSApp.terminate(nil) }
    @objc func openAccessibility() {
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary)
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    // MARK: system events

    func setupNotifications() {
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] _ in self?.tracker.tick() }
        ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(true, "sleep") }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(false, "wake") }
        ws.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(true, "screens off") }
        ws.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(false, "screens on") }
        ws.addObserver(forName: NSWorkspace.sessionDidResignActiveNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(true, "session inactive") }
        ws.addObserver(forName: NSWorkspace.sessionDidBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in self?.setLocked(false, "session active") }
        let dnc = DistributedNotificationCenter.default()
        dnc.addObserver(forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in self?.setLocked(true, "screen locked") }
        dnc.addObserver(forName: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in self?.setLocked(false, "screen unlocked") }
    }

    func setLocked(_ locked: Bool, _ why: String) {
        if tracker.locked != locked {
            tracker.locked = locked
            log(locked ? "locked: \(why)" : "unlocked: \(why)")
        }
        tracker.tick()
    }

    func setupSignals() {
        signal(SIGTERM, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        src.setEventHandler { [weak self] in
            self?.tracker.flush()
            log("SIGTERM received; exiting")
            exit(0)
        }
        src.resume()
        sigterm = src
    }

    // MARK: backup

    func backup() {
        guard let raw = store.setting("backup_dir"), !raw.isEmpty else { return }
        let dir = (raw as NSString).expandingTildeInPath
        do {
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            try store.vacuumInto(dir + "/tracker-backup.db")
            store.setSetting("last_backup", String(Int(Date().timeIntervalSince1970)))
            log("backup written to \(dir)/tracker-backup.db")
        } catch {
            log("backup failed: \(error)")
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
