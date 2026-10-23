import AppKit
import ApplicationServices
import CoreGraphics

func attr(_ e: AXUIElement, _ key: CFString) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, key, &value) == .success else { return nil }
    return value
}

func text(_ e: AXUIElement, _ key: CFString) -> String {
    attr(e, key) as? String ?? ""
}

func label(_ e: AXUIElement) -> String {
    let title = text(e, kAXTitleAttribute as CFString)
    if !title.isEmpty { return title }
    let value = text(e, kAXValueAttribute as CFString)
    if !value.isEmpty { return value }
    return text(e, kAXDescriptionAttribute as CFString)
}

func position(_ e: AXUIElement) -> CGPoint? {
    guard let raw = attr(e, kAXPositionAttribute as CFString) else { return nil }
    var p = CGPoint.zero
    guard AXValueGetValue(raw as! AXValue, .cgPoint, &p) else { return nil }
    return p
}

func size(_ e: AXUIElement) -> CGSize? {
    guard let raw = attr(e, kAXSizeAttribute as CFString) else { return nil }
    var s = CGSize.zero
    guard AXValueGetValue(raw as! AXValue, .cgSize, &s) else { return nil }
    return s
}

func children(_ e: AXUIElement) -> [AXUIElement] {
    attr(e, kAXChildrenAttribute as CFString) as? [AXUIElement] ?? []
}

func find(_ e: AXUIElement, _ predicate: (AXUIElement) -> Bool) -> AXUIElement? {
    if predicate(e) { return e }
    for child in children(e) {
        if let result = find(child, predicate) { return result }
    }
    return nil
}

guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.utmapp.UTMPumaFixed").first else {
    fputs("UTM 2 is not running\n", stderr)
    exit(1)
}
let root = AXUIElementCreateApplication(app.processIdentifier)
let command = CommandLine.arguments.dropFirst().first ?? "dump"

if command == "run" {
    let row = find(root) {
        text($0, kAXRoleAttribute as CFString) == "AXRow" &&
        find($0) { label($0).contains("OS X 10.1") } != nil
    }
    let button = row.flatMap { find($0) { text($0, kAXRoleAttribute as CFString) == "AXButton" } }
    guard let button else { exit(2) }
    let result = AXUIElementPerformAction(button, kAXPressAction as CFString)
    print("run result=\(result.rawValue)")
} else if command == "input" {
    app.activate(options: [.activateIgnoringOtherApps])
    usleep(500_000)
    let window = find(root) { text($0, kAXRoleAttribute as CFString) == "AXWindow" && text($0, kAXTitleAttribute as CFString).contains("Puma") }
    let p = window.flatMap(position) ?? CGPoint(x: 80, y: 300)
    let s = window.flatMap(size) ?? CGSize(width: 1060, height: 850)
    // Keep events inside the Metal display; the final click is the installer
    // Continue control for the standard 800x600 Puma installer screen.
    let points = [
        CGPoint(x: p.x + 140, y: p.y + 300),
        CGPoint(x: p.x + 280, y: p.y + 390),
        CGPoint(x: p.x + 420, y: p.y + 490),
        CGPoint(x: p.x + 560, y: p.y + 590),
        CGPoint(x: p.x + min(s.width - 160, 860), y: p.y + min(s.height - 150, 650)),
    ]
    func post(_ type: CGEventType, _ point: CGPoint, delta: CGPoint = .zero) {
        guard let event = CGEvent(mouseEventSource: nil, mouseType: type,
                                  mouseCursorPosition: point, mouseButton: .left) else { return }
        // AppKit derives NSEvent.deltaX/deltaY from these fields. Supplying
        // them explicitly makes the test deterministic even after UTM locks
        // and warps the host cursor for relative input.
        event.setIntegerValueField(.mouseEventDeltaX, value: Int64(delta.x.rounded()))
        event.setIntegerValueField(.mouseEventDeltaY, value: Int64(delta.y.rounded()))
        event.post(tap: .cgSessionEventTap)
    }
    var previous = points[0]
    post(.mouseMoved, previous)
    for point in points.dropFirst() {
        post(.mouseMoved, point, delta: CGPoint(x: point.x - previous.x, y: point.y - previous.y))
        previous = point
        usleep(80_000)
    }
    let click = points[points.count - 1]
    post(.leftMouseDown, click); usleep(100_000); post(.leftMouseUp, click)
    print("input window=\(p) size=\(s) click=\(click)")
} else if command == "click" {
    app.activate(options: [.activateIgnoringOtherApps])
    usleep(500_000)
    let window = find(root) { text($0, kAXRoleAttribute as CFString) == "AXWindow" && text($0, kAXTitleAttribute as CFString).contains("Puma") }
    let p = window.flatMap(position) ?? CGPoint(x: 59, y: 227)
    // The Puma display starts below UTM's 40-point toolbar. Continue is at
    // approximately (646,542) in the 800x600 guest framebuffer.
    let point = CGPoint(x: p.x + 646, y: p.y + 40 + 542)
    func post(_ type: CGEventType) {
        guard let event = CGEvent(mouseEventSource: nil, mouseType: type,
                                  mouseCursorPosition: point, mouseButton: .left) else { return }
        event.post(tap: .cgSessionEventTap)
    }
    post(.mouseMoved)
    post(.leftMouseDown)
    usleep(100_000)
    post(.leftMouseUp)
    print("click point=\(point)")
} else if command == "close" {
    let window = find(root) {
        text($0, kAXRoleAttribute as CFString) == "AXWindow" &&
        text($0, kAXTitleAttribute as CFString).contains("Puma")
    }
    guard let window,
          let closeButtonValue = attr(window, kAXCloseButtonAttribute as CFString) else {
        fputs("Puma window or close button not found\n", stderr)
        exit(3)
    }
    let closeButton = closeButtonValue as! AXUIElement
    let result = AXUIElementPerformAction(closeButton, kAXPressAction as CFString)
    print("close result=\(result.rawValue)")
} else {
    func dump(_ e: AXUIElement, _ depth: Int = 0) {
        let role = text(e, kAXRoleAttribute as CFString)
        let title = text(e, kAXTitleAttribute as CFString)
        if !role.isEmpty { print(String(repeating: "  ", count: depth) + "\(role) \(title) pos=\(String(describing: position(e))) size=\(String(describing: size(e)))") }
        guard depth < 10 else { return }
        for child in children(e) { dump(child, depth + 1) }
    }
    dump(root)
}
