#pragma once

#include "QemuEngine.hpp"
#include "SystemSoftwareEngine.hpp"
#include "VmConfig.hpp"

#include <QByteArray>
#include <QImage>
#include <QJsonArray>
#include <QProcess>
#include <QSet>
#include <QWidget>

#include <atomic>
#include <mutex>

namespace vw {
class MacMouseDeltaTracker;
}

class QKeyEvent;
class QLabel;
class QLocalSocket;
class QMouseEvent;
class QPushButton;
class QResizeEvent;
class QTimer;
class QWheelEvent;

namespace vw {

class VirtualMachineWindow : public QWidget {
    Q_OBJECT
public:
    explicit VirtualMachineWindow(VmConfig config, QWidget* parent = nullptr);
    ~VirtualMachineWindow() override;

    bool start(QString* error = nullptr);
    qint64 processId() const;
    bool isRunning() const;
    bool matchesConfig(const VmConfig& config) const;
    void activateRuntimeWindow();

signals:
    void configChanged(const vw::VmConfig& config);
    void firstFrameReady();
    void bootFailed(const QString& detail);

private:
    void resizeEvent(QResizeEvent* event) override;
    void keyPressEvent(QKeyEvent* event) override;
    void keyReleaseEvent(QKeyEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    bool eventFilter(QObject* watched, QEvent* event) override;

    void buildUi();
    void captureFrame();
    void togglePower();
    void rebootVm();
    void openImageMenu();
    void replaceSystemSoftwareFloppy(int slot);
    void ejectSystemSoftwareFloppy(int slot);
    void ejectImage();
    void replaceImage();
    bool handleKeyEvent(QKeyEvent* event, bool pressed);
    bool forwardKeyToQemu(QKeyEvent* event, bool pressed);
    void toggleFullscreenMode();
    void toggleMouseLock();
    void releaseInputCapture();
    void releasePressedKeys();
    void stopVm();
    bool sendMonitorCommand(const QString& command, QByteArray* reply = nullptr);
    bool forwardMouseButton(Qt::MouseButton button, bool pressed);
    bool forwardMouseMove(const QPoint& globalPosition);
    bool forwardMouseWheel(const QPoint& angleDelta);
    void flushMouseMove();
    bool usesAbsoluteTablet() const;
    bool mapAbsoluteMousePosition(const QPoint& globalPosition, QPoint* position) const;
    bool sendAbsoluteMousePosition(const QPoint& position);
    bool sendAbsoluteMouseButton(Qt::MouseButton button, bool pressed);
    bool sendAbsoluteMouseWheel(int steps);
    bool sendRelativeMouseMove(const QPoint& delta);
    bool sendQmpKeyEvent(const QString& key, bool pressed);
    bool sendQmpInputEvents(const QJsonArray& events);
    bool sendQmpScreenDump(const QString& path);
    bool sendQmpCommand(const QByteArray& command);
    bool takeNativeMouseDelta(QPoint* delta);
    void connectQmp();
    void processQmpData();
    void closeQmp();
    void requestResponsiveFrames();
    void updateImage(const QString& path);
    void rescaleLastFrame();
    void paintInProcessFrame(const vw::MvFrame& frame);
    QImage convertInProcessFrame(const vw::MvFrame& frame) const;
    QString findFirstCdromDevice(const QByteArray& infoBlock) const;
    QString framePath() const;
    int frameIntervalMs() const;
    bool shouldRecenterMouse(const QPoint& globalPosition) const;
    void recenterMouse();

    VmConfig config_;
    QemuEngine engine_;
    SystemSoftwareEngine systemSoftwareEngine_;
    QProcess process_;
    QTimer* frameTimer_ = nullptr;
    QTimer* mouseMoveTimer_ = nullptr;
    QTimer* qmpConnectTimer_ = nullptr;
    QTimer* responsiveFrameTimer_ = nullptr;
    QLocalSocket* qmpSocket_ = nullptr;
    MacMouseDeltaTracker* macMouseDeltaTracker_ = nullptr;
    QLabel* titleLabel_ = nullptr;
    QLabel* statusLabel_ = nullptr;
    QLabel* displayLabel_ = nullptr;
    QPushButton* powerButton_ = nullptr;
    QPushButton* rebootButton_ = nullptr;
    QPushButton* imageButton_ = nullptr;
    QPushButton* fullscreenButton_ = nullptr;
    QPushButton* mouseLockButton_ = nullptr;
    QImage lastFrame_;
    mutable std::mutex inProcessFrameMutex_;
    QImage pendingInProcessFrame_;
    std::atomic<bool> inProcessFramePending_{false};
    int captureAttempts_ = 0;
    bool firstFrameShown_ = false;
    bool frameCapturePending_ = false;
    bool retriedTcgAfterAccelerationFailure_ = false;
    bool absoluteTabletActive_ = false;
    bool qmpGreetingReceived_ = false;
    bool qmpCapabilitiesRequested_ = false;
    bool qmpCapabilitiesReady_ = false;
    bool userRequestedStop_ = false;
    bool systemSoftwareRestartPending_ = false;
    bool mouseLocked_ = false;
    bool suppressMouseLockHint_ = false;
    bool cursorOverrideActive_ = false;
    bool hasMousePosition_ = false;
    QPoint lastMousePosition_;
    QPoint mouseAnchor_;
    QPoint pendingMouseDelta_;
    QPoint pendingAbsoluteMousePosition_;
    QPoint displayedFrameOrigin_;
    QSize displayedFrameSize_;
    QString qmpSocketPath_;
    QByteArray qmpBuffer_;
    bool hasPendingAbsoluteMousePosition_ = false;
    int mouseButtonState_ = 0;
    QSet<QString> pressedKeys_;
};

} // namespace vw
