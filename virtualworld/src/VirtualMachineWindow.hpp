#pragma once

#include "QemuEngine.hpp"
#include "VmConfig.hpp"

#include <QImage>
#include <QProcess>
#include <QWidget>

class QKeyEvent;
class QLabel;
class QPushButton;
class QResizeEvent;
class QTimer;

namespace vw {

class VirtualMachineWindow : public QWidget {
public:
    explicit VirtualMachineWindow(VmConfig config, QWidget* parent = nullptr);
    ~VirtualMachineWindow() override;

    bool start(QString* error = nullptr);
    qint64 processId() const;

private:
    void resizeEvent(QResizeEvent* event) override;
    void keyPressEvent(QKeyEvent* event) override;

    void buildUi();
    void captureFrame();
    void togglePower();
    void rebootVm();
    void openImageMenu();
    void ejectImage();
    void replaceImage();
    void toggleFullscreenMode();
    void toggleMouseLock();
    void stopVm();
    bool sendMonitorCommand(const QString& command, QByteArray* reply = nullptr);
    void updateImage(const QString& path);
    void rescaleLastFrame();
    QString findFirstCdromDevice(const QByteArray& infoBlock) const;
    QString monitorPath() const;
    QString framePath() const;

    VmConfig config_;
    QemuEngine engine_;
    QProcess process_;
    QTimer* frameTimer_ = nullptr;
    QLabel* titleLabel_ = nullptr;
    QLabel* statusLabel_ = nullptr;
    QLabel* displayLabel_ = nullptr;
    QPushButton* powerButton_ = nullptr;
    QPushButton* rebootButton_ = nullptr;
    QPushButton* imageButton_ = nullptr;
    QPushButton* fullscreenButton_ = nullptr;
    QPushButton* mouseLockButton_ = nullptr;
    QImage lastFrame_;
    bool mouseLocked_ = false;
    bool suppressMouseLockHint_ = false;
};

} // namespace vw
