import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

struct Segment {
    var id: Int64
    var start: Double
    var kind: String          // active | idle | locked
    var bundle: String?
    var app: String?
    var title: String?
    var url: String?
    var lastHeartbeat: Double
}

/// Samples the frontmost app / window / browser tab once a second (plus on every app
/// activation) and turns the stream into contiguous "segments" in SQLite. All interpretation
/// (projects, categories, context switching) happens later in Python; this stays dumb and cheap.
final class Tracker {
    let store: Store
    private(set) var current: Segment?
    var idleThreshold: Double = 120
    var locked = false
    private(set) var paused = false
    private(set) var axTrusted = false

    private var lastTick: Double = 0
    private var lastSettingsRead: Double = 0
    private var lastFailureReset: Double = 0
    private var lastURLFetch: Double = 0
    private var urlFetchInFlight = false
    private var scriptFailures: [String: Int] = [:]
    private let scriptQueue = DispatchQueue(label: "tracker.applescript", qos: .utility)
    var onChange: (() -> Void)?

    static let heartbeatInterval = 5.0
    static let urlRecheckInterval = 15.0

    /// Browsers we know how to read the current tab URL from.
    static let browserBundles: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview",
        "com.google.Chrome", "com.google.Chrome.canary", "com.google.Chrome.beta", "com.google.Chrome.dev",
        "com.microsoft.edgemac", "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev",
        "com.brave.Browser", "com.brave.Browser.beta", "com.brave.Browser.nightly",
        "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "com.operasoftware.OperaGX",
        "company.thebrowser.Browser", "company.thebrowser.dia",
        "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly",
        "app.zen-browser.zen", "ai.perplexity.comet", "com.kagi.kagimacOS",
        "org.chromium.Chromium", "com.sigmaos.sigmaos",
    ]
    /// Browsers that answer `URL of active tab of front window` (Chromium family) or
    /// `URL of front document` (Safari) via AppleScript. Others fall back to Accessibility.
    static let scriptableBundles: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview",
        "com.google.Chrome", "com.google.Chrome.canary", "com.google.Chrome.beta", "com.google.Chrome.dev",
        "com.microsoft.edgemac", "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev",
        "com.brave.Browser", "com.brave.Browser.beta", "com.brave.Browser.nightly",
        "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "com.operasoftware.OperaGX",
        "company.thebrowser.Browser", "org.chromium.Chromium",
    ]

    init(store: Store) {
        self.store = store
        axTrusted = AXIsProcessTrusted()
        readSettings(force: true)
    }

    // MARK: settings

    func readSettings(force: Bool = false) {
        let now = Date().timeIntervalSince1970
        if !force && now - lastSettingsRead < 10 { return }
        lastSettingsRead = now
        if let s = store.setting("idle_threshold_seconds"), let v = Double(s), v >= 10 { idleThreshold = v }
        let p = (store.setting("paused") ?? "0") == "1"
        if p != paused {
            paused = p
            log(paused ? "paused (via settings)" : "resumed (via settings)")
            onChange?()
        }
    }

    func setPaused(_ p: Bool) {
        paused = p
        store.setSetting("paused", p ? "1" : "0")
        log(p ? "paused (menu)" : "resumed (menu)")
        tick()
        onChange?()
    }

    // MARK: sampling

    func tick() {
        let now = Date().timeIntervalSince1970
        readSettings()
        if now - lastFailureReset > 600 { scriptFailures.removeAll(); lastFailureReset = now }

        // A long gap between ticks means the machine was asleep: attribute it as 'locked'.
        if lastTick > 0, now - lastTick > 60, let c = current {
            let gapStart = c.lastHeartbeat
            close(at: gapStart)
            if !paused {
                open(kind: "locked", start: gapStart, bundle: nil, app: nil, title: nil, switchKind: "gap")
                close(at: now)
            }
        }
        lastTick = now

        if paused {
            if current != nil { close(at: now) }
            return
        }
        if !axTrusted {
            axTrusted = AXIsProcessTrusted()
            if axTrusted { log("accessibility permission granted; window titles enabled") }
        }

        let idle = Tracker.idleSeconds()
        let front = NSWorkspace.shared.frontmostApplication
        let atLoginWindow = front?.bundleIdentifier == "com.apple.loginwindow"
        let hardLocked = locked || atLoginWindow
        if hardLocked || idle >= idleThreshold {
            let kind = hardLocked ? "locked" : "idle"
            if let c = current, c.kind != "active" {
                heartbeat(now)
                return
            }
            // Idle began at the last input event, so trim the active segment back to that point.
            let idleStart = hardLocked ? now : max(now - idle, current?.start ?? 0)
            close(at: idleStart)
            open(kind: kind, start: idleStart, bundle: nil, app: nil, title: nil, switchKind: "idle")
            return
        }

        guard let app = front else { heartbeat(now); return }
        let bundle = app.bundleIdentifier ?? "pid.\(app.processIdentifier)"
        let name = app.localizedName ?? bundle
        let title = axTrusted ? Tracker.focusedWindowTitle(pid: app.processIdentifier) : nil
        let isBrowser = Tracker.browserBundles.contains(bundle)

        var switchKind: String? = nil
        if let c = current {
            if c.kind != "active" { switchKind = "resume" }
            else if c.bundle != bundle { switchKind = "app" }
            else if let t = title, t != c.title { switchKind = isBrowser ? "tab" : "window" }
        } else {
            switchKind = "start"
        }

        if let sk = switchKind {
            close(at: now)
            open(kind: "active", start: now, bundle: bundle, app: name, title: title, switchKind: sk)
            if isBrowser { fetchURL(bundle: bundle, pid: app.processIdentifier) }
        } else {
            heartbeat(now)
            if isBrowser, now - lastURLFetch > Tracker.urlRecheckInterval {
                fetchURL(bundle: bundle, pid: app.processIdentifier)
            }
        }
    }

    private func heartbeat(_ now: Double) {
        guard var c = current else { return }
        if now - c.lastHeartbeat >= Tracker.heartbeatInterval {
            store.setEnd(id: c.id, end: now)
            c.lastHeartbeat = now
            current = c
        }
    }

    func close(at time: Double) {
        guard let c = current else { return }
        store.setEnd(id: c.id, end: max(c.start, time))
        current = nil
    }

    private func open(kind: String, start: Double, bundle: String?, app: String?, title: String?, switchKind: String) {
        let id = store.insertSegment(start: start, end: start, kind: kind, app: app, bundle: bundle,
                                     title: title, url: nil, domain: nil, switchKind: switchKind)
        current = Segment(id: id, start: start, kind: kind, bundle: bundle, app: app, title: title, url: nil, lastHeartbeat: start)
        onChange?()
    }

    func flush() {
        let now = Date().timeIntervalSince1970
        if let c = current { store.setEnd(id: c.id, end: now) }
    }

    // MARK: browser URLs

    private func fetchURL(bundle: String, pid: pid_t) {
        lastURLFetch = Date().timeIntervalSince1970
        if urlFetchInFlight { return }
        guard let segmentID = current?.id else { return }
        urlFetchInFlight = true
        let useScript = Tracker.scriptableBundles.contains(bundle) && (scriptFailures[bundle] ?? 0) < 3
        let trusted = axTrusted
        scriptQueue.async { [weak self] in
            var url: String? = nil
            var scriptErr: String? = nil
            if useScript { (url, scriptErr) = Tracker.appleScriptURL(bundle: bundle) }
            if url == nil && trusted { url = Tracker.axURL(pid: pid, bundle: bundle) }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.urlFetchInFlight = false
                if let e = scriptErr {
                    self.scriptFailures[bundle, default: 0] += 1
                    if self.scriptFailures[bundle] == 1 { log("applescript \(bundle): \(e) (falling back to accessibility)") }
                } else if useScript {
                    self.scriptFailures[bundle] = 0
                }
                self.applyURL(url, segmentID: segmentID, bundle: bundle, pid: pid)
            }
        }
    }

    private func applyURL(_ url: String?, segmentID: Int64, bundle: String, pid: pid_t) {
        guard var c = current, c.kind == "active" else { return }
        if c.id != segmentID {
            // Segment changed while we were asking; ask again for the new one if it still needs a URL.
            if c.url == nil, c.bundle == bundle { fetchURL(bundle: bundle, pid: pid) }
            return
        }
        guard let url = url, !url.isEmpty else { return }
        let domain = Tracker.domain(of: url)
        if c.url == nil {
            c.url = url
            current = c
            store.setURL(id: c.id, url: url, domain: domain)
            onChange?()
        } else if c.url != url {
            // Same window title, different page (single-page apps): start a new segment.
            let now = Date().timeIntervalSince1970
            close(at: now)
            open(kind: "active", start: now, bundle: c.bundle, app: c.app, title: c.title, switchKind: "tab")
            current?.url = url
            if let id = current?.id { store.setURL(id: id, url: url, domain: domain) }
        }
    }

    static func appleScriptURL(bundle: String) -> (String?, String?) {
        let body: String
        if bundle.hasPrefix("com.apple.Safari") {
            body = "tell application id \"\(bundle)\" to return URL of front document"
        } else {
            body = "tell application id \"\(bundle)\" to return URL of active tab of front window"
        }
        let source = "with timeout of 3 seconds\n\(body)\nend timeout"
        guard let script = NSAppleScript(source: source) else { return (nil, "compile failed") }
        var err: NSDictionary?
        let out = script.executeAndReturnError(&err)
        if let e = err {
            let num = e[NSAppleScript.errorNumber] ?? ""
            let msg = e[NSAppleScript.errorMessage] ?? ""
            return (nil, "\(num) \(msg)")
        }
        return (out.stringValue, nil)
    }

    static func domain(of url: String) -> String? {
        guard let u = URL(string: url), var host = u.host?.lowercased() else {
            // Chrome's omnibox may hand back "example.com/path" without a scheme.
            if let u = URL(string: "https://" + url), let h = u.host { return h.lowercased().replacingOccurrences(of: "www.", with: "") }
            return nil
        }
        if host.hasPrefix("www.") { host.removeFirst(4) }
        return host
    }

    // MARK: Accessibility helpers

    static func idleSeconds() -> Double {
        return CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
    }

    static func focusedWindow(_ appEl: AXUIElement) -> AXUIElement? {
        var win: CFTypeRef?
        var r = AXUIElementCopyAttributeValue(appEl, kAXFocusedWindowAttribute as CFString, &win)
        if r != .success { r = AXUIElementCopyAttributeValue(appEl, kAXMainWindowAttribute as CFString, &win) }
        guard r == .success, let w = win else { return nil }
        return (w as! AXUIElement)
    }

    static func axString(_ el: AXUIElement, _ attr: String) -> String? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success, let val = v else { return nil }
        if let s = val as? String { return s }
        if let u = val as? URL { return u.absoluteString }
        return nil
    }

    static func axChildren(_ el: AXUIElement) -> [AXUIElement] {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &v) == .success,
              let arr = v as? NSArray else { return [] }
        var out: [AXUIElement] = []
        for item in arr { out.append(item as! AXUIElement) }
        return out
    }

    static func focusedWindowTitle(pid: pid_t) -> String? {
        let appEl = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appEl, 0.5)
        guard let win = focusedWindow(appEl) else { return nil }
        return axString(win, kAXTitleAttribute as String)
    }

    /// Read the current URL straight out of the browser's address bar via Accessibility.
    /// Used for browsers without AppleScript support (Firefox family) or when Automation is denied.
    static func axURL(pid: pid_t, bundle: String) -> String? {
        let appEl = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appEl, 0.5)
        guard let win = focusedWindow(appEl) else { return nil }
        if bundle.hasPrefix("com.apple.Safari") {
            if let doc = axString(win, kAXDocumentAttribute as String), !doc.isEmpty { return doc }
        }
        var queue: [(AXUIElement, Int)] = [(win, 0)]
        var visited = 0
        while !queue.isEmpty && visited < 800 {
            let (el, depth) = queue.removeFirst()
            visited += 1
            let role = axString(el, kAXRoleAttribute as String) ?? ""
            if role == "AXWebArea" || role == "AXScrollArea" || role == "AXMenuBar" { continue }
            if role == "AXTextField" || role == "AXComboBox" {
                let desc = [kAXDescriptionAttribute, kAXTitleAttribute, kAXIdentifierAttribute, kAXPlaceholderValueAttribute]
                    .compactMap { axString(el, $0 as String) }
                    .joined(separator: " ")
                    .lowercased()
                if desc.contains("address") || desc.contains("url") || desc.contains("omnibox") {
                    if let v = axString(el, kAXValueAttribute as String), !v.isEmpty, !v.contains(" ") {
                        return v.contains("://") ? v : "https://" + v
                    }
                }
            }
            if depth < 14 {
                for ch in axChildren(el) { queue.append((ch, depth + 1)) }
            }
        }
        return nil
    }
}
