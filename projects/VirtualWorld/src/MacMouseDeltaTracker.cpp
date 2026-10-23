#include "MacMouseDeltaTracker.hpp"

#include <QWidget>

namespace vw {

bool activateExternalApplication(qint64) {
    return false;
}

void MacMouseDeltaTracker::setTarget(QWidget*) {
    targetWindow_ = nullptr;
    reset();
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

bool MacMouseDeltaTracker::nativeEventFilter(const QByteArray&, void*, NativeEventResult*) {
    return false;
}

} // namespace vw
