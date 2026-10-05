import Foundation

private let logFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func log(_ message: String) {
    print("\(logFormatter.string(from: Date())) \(message)")
    fflush(stdout)
}

enum Paths {
    static var dataDir: String {
        if let e = ProcessInfo.processInfo.environment["TRACKER_DATA_DIR"], !e.isEmpty {
            return (e as NSString).expandingTildeInPath
        }
        return NSHomeDirectory() + "/Library/Application Support/Tracker"
    }
    static var dbPath: String {
        if let e = ProcessInfo.processInfo.environment["TRACKER_DB"], !e.isEmpty {
            return (e as NSString).expandingTildeInPath
        }
        return dataDir + "/tracker.db"
    }
}
