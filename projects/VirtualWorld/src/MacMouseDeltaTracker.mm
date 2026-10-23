#include "MacMouseDeltaTracker.hpp"

#include <AppKit/NSEvent.h>
#include <AppKit/NSRunningApplication.h>
#include <AppKit/NSView.h>
#include <AppKit/NSWindow.h>

#include <QWidget>

namespace vw {

bool activateExternalApplication(qint64 processId) {
    if (processId <= 0) {
        return false;
    }
    NSRunningApplication* application =
        [NSRunningApplication runningApplicationWithProcessIdentifier:static_cast<pid_t>(processId)];
    if (!application || application.terminated) {
        return false;
    }
    return [application activateWithOptions:NSApplicationActivateIgnoringOtherApps
                                             | NSApplicationActivateAllWindows];
}

void MacMouseDeltaTracker::setTarget(QWidget* widget) {
    targetWindow_ = nullptr;
    reset();
    if (!widget) {
        return;
    }

    QWidget* window = widget->window();
    if (!window) {
        return;
    }
    NSView* view = reinterpret_cast<NSView*>(window->winId());
    if (view) {
        targetWindow_ = [view window];
    }
}

void MacMouseDeltaTracker::setEnabled(bool enabled) {
    enabled_ = enabled;
    reset();
}

void MacMouseDeltaTracker::reset() {
    pendingDelta_ = {};
    hasPendingEvent_ = false;
}

bool MacMouseDeltaTracker::takeDelta(QPoint* delta) {
    if (!delta || !hasPendingEvent_) {
        return false;
    }
    *delta = pendingDelta_;
    reset();
    return true;
}

bool MacMouseDeltaTracker::nativeEventFilter(const QByteArray& eventType,
                                             void* message,
                                             NativeEventResult*) {
    if (!enabled_ || eventType != "mac_generic_NSEvent" || !message) {
        return false;
    }

    NSEvent* event = reinterpret_cast<NSEvent*>(message);
    if (targetWindow_ && [event window] != targetWindow_) {
        return false;
    }

    switch ([event type]) {
    case NSEventTypeMouseMoved:
    case NSEventTypeLeftMouseDragged:
    case NSEventTypeRightMouseDragged:
    case NSEventTypeOtherMouseDragged:
        break;
    default:
        return false;
    }

    // Match QEMU's Cocoa frontend: it forwards the native integer deltas
    // directly instead of deriving movement from an accelerated screen cursor.
    pendingDelta_ += QPoint(static_cast<int>([event deltaX]),
                            static_cast<int>([event deltaY]));
    hasPendingEvent_ = true;
    return false;
}

} // namespace vw
