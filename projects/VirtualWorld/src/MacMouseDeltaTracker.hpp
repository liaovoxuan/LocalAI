#pragma once

#include <QAbstractNativeEventFilter>
#include <QPoint>
#include <QtGlobal>

class QWidget;

namespace vw {

bool activateExternalApplication(qint64 processId);

class MacMouseDeltaTracker final : public QAbstractNativeEventFilter {
public:
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    using NativeEventResult = qintptr;
#else
    using NativeEventResult = long;
#endif

    MacMouseDeltaTracker() = default;
    ~MacMouseDeltaTracker() override = default;

    void setTarget(QWidget* widget);
    void setEnabled(bool enabled);
    void reset();
    bool takeDelta(QPoint* delta);

    bool nativeEventFilter(const QByteArray& eventType, void* message, NativeEventResult* result) override;

private:
    void* targetWindow_ = nullptr;
    QPoint pendingDelta_;
    bool hasPendingEvent_ = false;
    bool enabled_ = false;
};

} // namespace vw
