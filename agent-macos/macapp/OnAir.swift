// onair — menu-bar wrapper around the Python agent.
//
// Exists because the agent's hard part is not running it, it is the four macOS
// grants it needs. A terminal command gives a stranger no way to discover that
// Accessibility is missing or that Chrome's JS bridge is off — they just see
// controls that silently do nothing. This surfaces every check with a link to
// the right settings pane, and a QR code so pairing the iPad needs no typing.
//
// Deliberately built with swiftc alone — no Xcode project, so `make` works on
// a machine with only the Command Line Tools.

import Cocoa
import CoreImage

final class Agent {
    private var process: Process?
    private(set) var url: String = ""

    var isRunning: Bool { process?.isRunning ?? false }

    /// Repo root, derived from the executable's location inside the bundle.
    static func repoRoot() -> String {
        let fm = FileManager.default
        func holdsAgent(_ path: String) -> Bool {
            fm.fileExists(atPath: path + "/agent/server.py")
        }
        // Bundled: Contents/MacOS/OnAir -> Contents/Resources.
        if let res = Bundle.main.resourcePath, holdsAgent(res) { return res }
        // Unbundled: walk up from the binary to find the repo checkout.
        var dir = URL(fileURLWithPath: CommandLine.arguments[0])
            .resolvingSymlinksInPath().deletingLastPathComponent()
        for _ in 0..<5 {
            if holdsAgent(dir.path) { return dir.path }
            dir = dir.deletingLastPathComponent()
        }
        return fm.currentDirectoryPath
    }

    func start() {
        guard !isRunning else { return }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", "-m", "agent.server"]
        task.currentDirectoryURL = URL(fileURLWithPath: Agent.repoRoot())

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            guard let text = String(data: handle.availableData, encoding: .utf8),
                  !text.isEmpty else { return }
            // The agent prints the pairing URL on startup; capture it for the
            // menu and the QR code rather than making the user read a log.
            for line in text.split(separator: "\n") {
                if line.contains("http://"), line.contains("?t=") {
                    let found = line.trimmingCharacters(in: .whitespaces)
                    DispatchQueue.main.async { self?.url = found }
                }
            }
            FileHandle.standardError.write(text.data(using: .utf8)!)
        }
        do { try task.run(); process = task } catch { NSLog("onair: %@", "\(error)") }
    }

    func stop() {
        process?.terminate()
        process = nil
        url = ""
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let agent = Agent()
    private var qrWindow: NSWindow?
    private var checksWindow: NSWindow?

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "◉"
        statusItem.button?.toolTip = "onair"
        agent.start()
        rebuildMenu()
        Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.rebuildMenu()
        }
    }

    func applicationWillTerminate(_ note: Notification) { agent.stop() }

    private func rebuildMenu() {
        statusItem.button?.title = agent.isRunning ? "◉" : "○"

        let menu = NSMenu()
        let state = agent.isRunning ? "Running" : "Stopped"
        let header = NSMenuItem(title: "onair — \(state)", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        if !agent.url.isEmpty {
            menu.addItem(withTitle: "Open panel", action: #selector(openPanel), keyEquivalent: "o")
                .target = self
            menu.addItem(withTitle: "Pair iPad (QR code)…", action: #selector(showQR), keyEquivalent: "p")
                .target = self
            menu.addItem(withTitle: "Copy pairing link", action: #selector(copyURL), keyEquivalent: "c")
                .target = self
            menu.addItem(.separator())
        }

        menu.addItem(withTitle: "Check permissions…", action: #selector(showChecks), keyEquivalent: "d")
            .target = self
        menu.addItem(withTitle: agent.isRunning ? "Stop agent" : "Start agent",
                     action: #selector(toggleAgent), keyEquivalent: "")
            .target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit onair", action: #selector(quit), keyEquivalent: "q")
            .target = self
        statusItem.menu = menu
    }

    // ── actions ──────────────────────────────────────────────────────────────

    @objc private func openPanel() {
        if let u = URL(string: agent.url) { NSWorkspace.shared.open(u) }
    }

    @objc private func copyURL() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(agent.url, forType: .string)
    }

    @objc private func toggleAgent() {
        agent.isRunning ? agent.stop() : agent.start()
        rebuildMenu()
    }

    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func showQR() {
        guard let image = qrImage(agent.url) else { return }
        let view = NSImageView(frame: NSRect(x: 20, y: 60, width: 280, height: 280))
        view.image = image

        let label = NSTextField(wrappingLabelWithString:
            "Scan with the iPad camera, open the link in Safari,\n"
            + "then Share > Add to Home Screen.")
        label.frame = NSRect(x: 20, y: 12, width: 280, height: 40)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .secondaryLabelColor
        label.alignment = .center

        let window = panelWindow(title: "Pair iPad", width: 320, height: 360)
        window.contentView?.addSubview(view)
        window.contentView?.addSubview(label)
        qrWindow = window
        present(window)
    }

    @objc private func showChecks() {
        let output = runDoctor()
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 620, height: 300))
        text.string = output
        text.isEditable = false
        text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        text.textContainerInset = NSSize(width: 14, height: 14)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 620, height: 300))
        scroll.documentView = text
        scroll.hasVerticalScroller = true

        let window = panelWindow(title: "onair — permission checks", width: 620, height: 300)
        window.contentView?.addSubview(scroll)
        checksWindow = window
        present(window)
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private func present(_ window: NSWindow) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func panelWindow(title: String, width: CGFloat, height: CGFloat) -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = title
        window.center()
        window.isReleasedWhenClosed = false
        return window
    }

    private func runDoctor() -> String {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", "-m", "agent.doctor"]
        task.currentDirectoryURL = URL(fileURLWithPath: Agent.repoRoot())
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        do { try task.run() } catch { return "could not run checks: \(error)" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }

    private func qrImage(_ string: String) -> NSImage? {
        guard let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(string.data(using: .utf8), forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: NSSize(width: 280, height: 280))
        image.addRepresentation(rep)
        return image
    }
}

// ── one-shot media-key mode ──────────────────────────────────────────────────
//
// `OnAir --mediakey up 3 fine` posts the event and exits without a menu bar.
//
// This exists because of who TCC blames. Accessibility is granted to *this app*,
// but the event was being posted by a separate unsigned helper binary, and macOS
// evaluates the grant against the posting process. Run from a terminal the
// helper inherited the terminal's grant and worked; run under the app it did
// not, so every write silently changed nothing. Posting from this executable
// means the process holding the grant is the one synthesising the event.
func postMediaKey(_ name: String, _ count: Int, _ fine: Bool) {
    let keys: [String: Int32] = [
        "up": 0,          // NX_KEYTYPE_SOUND_UP
        "down": 1,        // NX_KEYTYPE_SOUND_DOWN
        "mute": 7,        // NX_KEYTYPE_MUTE
        "brightup": 2,    // NX_KEYTYPE_BRIGHTNESS_UP
        "brightdown": 3,  // NX_KEYTYPE_BRIGHTNESS_DOWN
    ]
    guard let key = keys[name] else { return }

    func post(_ down: Bool) {
        var flags: NSEvent.ModifierFlags = []
        if fine { flags.insert(.shift); flags.insert(.option) }
        let raw = UInt(down ? 0xA00 : 0xB00) | UInt(flags.rawValue)
        guard let event = NSEvent.otherEvent(
            with: .systemDefined, location: .zero,
            modifierFlags: NSEvent.ModifierFlags(rawValue: raw),
            timestamp: 0, windowNumber: 0, context: nil, subtype: 8,
            data1: Int((key << 16) | ((down ? 0xA : 0xB) << 8)), data2: -1)
        else { return }
        event.cgEvent?.post(tap: .cghidEventTap)
    }

    for _ in 0..<max(1, count) {
        post(true); post(false)
        usleep(40_000)
    }
}

let args = CommandLine.arguments
if let i = args.firstIndex(of: "--mediakey"), args.count > i + 1 {
    let name = args[i + 1]
    let count = (args.count > i + 2) ? (Int(args[i + 2]) ?? 1) : 1
    let fine = args.contains("fine")
    postMediaKey(name, count, fine)
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // menu-bar only, no Dock icon
app.run()
