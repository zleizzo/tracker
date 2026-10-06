import Foundation
import SQLite3

let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

/// Thin SQLite wrapper. The daemon only ever writes `segments` and reads/writes `settings`;
/// everything else in the schema is owned by the Python side, but we create it here too so
/// either process can start first.
final class Store {
    enum StoreError: Error, CustomStringConvertible {
        case open(String), exec(String), prepare(String)
        var description: String {
            switch self {
            case .open(let m): return "open: \(m)"
            case .exec(let m): return "exec: \(m)"
            case .prepare(let m): return "prepare: \(m)"
            }
        }
    }

    static let schema = """
    CREATE TABLE IF NOT EXISTS segments (
      id INTEGER PRIMARY KEY,
      start REAL NOT NULL,
      end REAL NOT NULL,
      kind TEXT NOT NULL DEFAULT 'active',
      app TEXT,
      bundle TEXT,
      title TEXT,
      url TEXT,
      domain TEXT,
      switch_kind TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start);
    CREATE INDEX IF NOT EXISTS idx_segments_end ON segments(end);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS projects (
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      color INTEGER NOT NULL DEFAULT 0,
      category TEXT NOT NULL DEFAULT 'productive',
      archived INTEGER NOT NULL DEFAULT 0,
      created_at REAL,
      estimate_hours REAL,
      done INTEGER NOT NULL DEFAULT 0,
      completed_at REAL
    );
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY,
      project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      done INTEGER NOT NULL DEFAULT 0,
      created_at REAL,
      estimate_hours REAL,
      completed_at REAL
    );
    CREATE TABLE IF NOT EXISTS rules (
      id INTEGER PRIMARY KEY,
      field TEXT NOT NULL,
      pattern TEXT NOT NULL,
      is_regex INTEGER NOT NULL DEFAULT 0,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      category TEXT,
      priority INTEGER NOT NULL DEFAULT 0,
      created_at REAL
    );
    CREATE TABLE IF NOT EXISTS manual_entries (
      id INTEGER PRIMARY KEY,
      start REAL NOT NULL,
      end REAL NOT NULL,
      title TEXT NOT NULL,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      category TEXT,
      note TEXT NOT NULL DEFAULT '',
      created_at REAL
    );
    CREATE TABLE IF NOT EXISTS plan_blocks (
      id INTEGER PRIMARY KEY,
      day TEXT NOT NULL,
      start REAL NOT NULL,
      end REAL NOT NULL,
      project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      note TEXT NOT NULL DEFAULT '',
      created_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_blocks_day ON plan_blocks(day);
    CREATE TABLE IF NOT EXISTS focus_sessions (
      id INTEGER PRIMARY KEY,
      mode TEXT NOT NULL,
      start REAL NOT NULL,
      end REAL NOT NULL,
      locked INTEGER NOT NULL DEFAULT 0,
      stopped_at REAL,
      created_at REAL
    );
    CREATE TABLE IF NOT EXISTS focus_sites (
      id INTEGER PRIMARY KEY,
      list TEXT NOT NULL,
      pattern TEXT NOT NULL,
      created_at REAL
    );
    CREATE TABLE IF NOT EXISTS assignments (
      id INTEGER PRIMARY KEY,
      start REAL NOT NULL,
      end REAL NOT NULL,
      bundle TEXT,
      project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      category TEXT,
      note TEXT NOT NULL DEFAULT '',
      created_at REAL
    );
    """

    private var db: OpaquePointer?
    private var insertStmt: OpaquePointer?
    private var endStmt: OpaquePointer?
    private var urlStmt: OpaquePointer?
    let path: String

    init(path: String) throws {
        self.path = path
        let dir = (path as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        var handle: OpaquePointer?
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        if sqlite3_open_v2(path, &handle, flags, nil) != SQLITE_OK {
            let msg = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            throw StoreError.open(msg)
        }
        db = handle
        try exec("PRAGMA journal_mode=WAL;")
        try exec("PRAGMA synchronous=NORMAL;")
        try exec("PRAGMA busy_timeout=5000;")
        try exec(Store.schema)
        insertStmt = try prepare("INSERT INTO segments(start,end,kind,app,bundle,title,url,domain,switch_kind) VALUES(?,?,?,?,?,?,?,?,?)")
        endStmt = try prepare("UPDATE segments SET end=? WHERE id=?")
        urlStmt = try prepare("UPDATE segments SET url=?, domain=? WHERE id=?")
    }

    deinit {
        sqlite3_finalize(insertStmt)
        sqlite3_finalize(endStmt)
        sqlite3_finalize(urlStmt)
        sqlite3_close(db)
    }

    func exec(_ sql: String) throws {
        var err: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(db, sql, nil, nil, &err) != SQLITE_OK {
            let m = err.map { String(cString: $0) } ?? "unknown"
            sqlite3_free(err)
            throw StoreError.exec(m)
        }
    }

    private func prepare(_ sql: String) throws -> OpaquePointer {
        var s: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &s, nil) == SQLITE_OK, let st = s else {
            throw StoreError.prepare(String(cString: sqlite3_errmsg(db)))
        }
        return st
    }

    private func bind(_ st: OpaquePointer, _ i: Int32, _ v: String?) {
        if let v = v { sqlite3_bind_text(st, i, v, -1, SQLITE_TRANSIENT) } else { sqlite3_bind_null(st, i) }
    }

    func insertSegment(start: Double, end: Double, kind: String, app: String?, bundle: String?,
                       title: String?, url: String?, domain: String?, switchKind: String) -> Int64 {
        guard let st = insertStmt else { return 0 }
        sqlite3_reset(st)
        sqlite3_clear_bindings(st)
        sqlite3_bind_double(st, 1, start)
        sqlite3_bind_double(st, 2, end)
        bind(st, 3, kind)
        bind(st, 4, app)
        bind(st, 5, bundle)
        bind(st, 6, title)
        bind(st, 7, url)
        bind(st, 8, domain)
        bind(st, 9, switchKind)
        if sqlite3_step(st) != SQLITE_DONE {
            log("insert failed: \(String(cString: sqlite3_errmsg(db)))")
            return 0
        }
        return sqlite3_last_insert_rowid(db)
    }

    func setEnd(id: Int64, end: Double) {
        guard let st = endStmt, id > 0 else { return }
        sqlite3_reset(st)
        sqlite3_bind_double(st, 1, end)
        sqlite3_bind_int64(st, 2, id)
        if sqlite3_step(st) != SQLITE_DONE { log("setEnd failed: \(String(cString: sqlite3_errmsg(db)))") }
    }

    func setURL(id: Int64, url: String?, domain: String?) {
        guard let st = urlStmt, id > 0 else { return }
        sqlite3_reset(st)
        sqlite3_clear_bindings(st)
        bind(st, 1, url)
        bind(st, 2, domain)
        sqlite3_bind_int64(st, 3, id)
        if sqlite3_step(st) != SQLITE_DONE { log("setURL failed: \(String(cString: sqlite3_errmsg(db)))") }
    }

    func setting(_ key: String) -> String? {
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT value FROM settings WHERE key=?", -1, &st, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(st) }
        sqlite3_bind_text(st, 1, key, -1, SQLITE_TRANSIENT)
        if sqlite3_step(st) == SQLITE_ROW, let c = sqlite3_column_text(st, 0) { return String(cString: c) }
        return nil
    }

    func setSetting(_ key: String, _ value: String) {
        var st: OpaquePointer?
        let sql = "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { return }
        defer { sqlite3_finalize(st) }
        sqlite3_bind_text(st, 1, key, -1, SQLITE_TRANSIENT)
        sqlite3_bind_text(st, 2, value, -1, SQLITE_TRANSIENT)
        _ = sqlite3_step(st)
    }

    /// Consistent snapshot copy of the whole database (safe while the daemon keeps writing).
    func vacuumInto(_ dest: String) throws {
        let tmp = dest + ".tmp"
        try? FileManager.default.removeItem(atPath: tmp)
        let escaped = tmp.replacingOccurrences(of: "'", with: "''")
        try exec("VACUUM INTO '\(escaped)'")
        let fm = FileManager.default
        if fm.fileExists(atPath: dest) {
            _ = try fm.replaceItemAt(URL(fileURLWithPath: dest), withItemAt: URL(fileURLWithPath: tmp))
        } else {
            try fm.moveItem(atPath: tmp, toPath: dest)
        }
    }
}
