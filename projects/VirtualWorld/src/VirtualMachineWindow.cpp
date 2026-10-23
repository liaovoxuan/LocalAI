#include "VirtualMachineWindow.hpp"

#include "MacMouseDeltaTracker.hpp"

#include <QApplication>
#include <QAction>
#include <QDir>
#include <QDialog>
#include <QEvent>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLocalSocket>
#include <QMenu>
#include <QMessageBox>
#include <QMouseEvent>
#include <QPixmap>
#include <QPushButton>
#include <QRegularExpression>
#include <QCursor>
#include <QResizeEvent>
#include <QScreen>
#include <QSet>
#include <QSizePolicy>
#include <QStandardPaths>
#include <QStyle>
#include <QTimer>
#include <QtGlobal>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <QMetaObject>
#include <QPointer>

namespace vw {

namespace {

QString vmTitle(const VmConfig& config) {
    const QString name = config.name.trimmed().isEmpty() ? QStringLiteral("VirtualWorld VM") : config.name.trimmed();
    return QStringLiteral("%1 · %2").arg(name, normalizeArchitecture(config.architecture));
}

QString hmpQuote(QString value) {
    value.replace("\\", "\\\\");
    value.replace("\"", "\\\"");
    return "\"" + value + "\"";
}

QString monitorErrorSummary(const QByteArray& reply) {
    QString text = QString::fromUtf8(reply);
    text.remove(QRegularExpression("\x1B\\[[0-?]*[ -/]*[@-~]"));
    text.remove('\r');

    const QStringList lines = text.split('\n', Qt::SkipEmptyParts);
    for (int index = lines.size() - 1; index >= 0; --index) {
        const QString line = lines.at(index).trimmed();
        const int errorOffset = line.indexOf("Error:", Qt::CaseInsensitive);
        if (errorOffset >= 0) {
            return line.mid(errorOffset);
        }
    }
    return {};
}

QPushButton* makeToolButton(const QString& text, const QString& tooltip) {
    auto* button = new QPushButton(text);
    button->setToolTip(tooltip);
    button->setFixedSize(42, 36);
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

QPoint globalMousePosition(QMouseEvent* event) {
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    return event->globalPosition().toPoint();
#else
    return event->globalPos();
#endif
}

bool looksLikeUninitializedFrame(const QImage& image) {
    if (image.isNull() || image.width() < 80 || image.height() < 60) {
        return false;
    }
    QSet<QRgb> colors;
    int saturated = 0;
    int sampled = 0;
    const int stepX = qMax(1, image.width() / 80);
    const int stepY = qMax(1, image.height() / 50);
    for (int y = 0; y < image.height(); y += stepY) {
        for (int x = 0; x < image.width(); x += stepX) {
            const QRgb rgb = image.pixel(x, y);
            colors.insert(rgb);
            const int maxChannel = qMax(qRed(rgb), qMax(qGreen(rgb), qBlue(rgb)));
            const int minChannel = qMin(qRed(rgb), qMin(qGreen(rgb), qBlue(rgb)));
            if (maxChannel > 150 && maxChannel - minChannel > 100) {
                ++saturated;
            }
            ++sampled;
        }
    }
    if (sampled == 0) {
        return false;
    }
    const double saturatedRatio = double(saturated) / double(sampled);
    return colors.size() > 180 && saturatedRatio > 0.38;
}

bool looksLikeInactiveDisplayPlaceholder(const QImage& image) {
    if (image.isNull() || image.width() < 160 || image.height() < 120) {
        return false;
    }
    int nonBlack = 0;
    int grayTextLike = 0;
    int sampled = 0;
    const int stepX = qMax(1, image.width() / 120);
    const int stepY = qMax(1, image.height() / 80);
    for (int y = 0; y < image.height(); y += stepY) {
        for (int x = 0; x < image.width(); x += stepX) {
            const QRgb rgb = image.pixel(x, y);
            const int r = qRed(rgb);
            const int g = qGreen(rgb);
            const int b = qBlue(rgb);
            const int maxChannel = qMax(r, qMax(g, b));
            const int minChannel = qMin(r, qMin(g, b));
            if (maxChannel > 22) {
                ++nonBlack;
                if (maxChannel < 210 && maxChannel - minChannel < 28) {
                    ++grayTextLike;
                }
            }
            ++sampled;
        }
    }
    if (sampled == 0 || nonBlack == 0) {
        return false;
    }
    const double nonBlackRatio = double(nonBlack) / double(sampled);
    const double grayRatio = double(grayTextLike) / double(nonBlack);
    return nonBlackRatio > 0.002 && nonBlackRatio < 0.035 && grayRatio > 0.82;
}

QString qemuKeyName(QKeyEvent* event) {
    switch (event->key()) {
    case Qt::Key_Shift:
        return "shift";
    case Qt::Key_Control:
        return "ctrl";
    case Qt::Key_Alt:
        return "alt";
    case Qt::Key_Meta:
        return "meta_l";
    case Qt::Key_CapsLock:
        return "caps_lock";
    case Qt::Key_NumLock:
        return "num_lock";
    case Qt::Key_ScrollLock:
        return "scroll_lock";
    case Qt::Key_Return:
    case Qt::Key_Enter:
        return "ret";
    case Qt::Key_Escape:
        return "esc";
    case Qt::Key_Backspace:
        return "backspace";
    case Qt::Key_Tab:
        return "tab";
    case Qt::Key_Space:
        return "spc";
    case Qt::Key_Up:
        return "up";
    case Qt::Key_Down:
        return "down";
    case Qt::Key_Left:
        return "left";
    case Qt::Key_Right:
        return "right";
    case Qt::Key_Delete:
        return "delete";
    case Qt::Key_Home:
        return "home";
    case Qt::Key_End:
        return "end";
    case Qt::Key_PageUp:
        return "pgup";
    case Qt::Key_PageDown:
        return "pgdn";
    case Qt::Key_Insert:
        return "insert";
    case Qt::Key_F1:
        return "f1";
    case Qt::Key_F2:
        return "f2";
    case Qt::Key_F3:
        return "f3";
    case Qt::Key_F4:
        return "f4";
    case Qt::Key_F5:
        return "f5";
    case Qt::Key_F6:
        return "f6";
    case Qt::Key_F7:
        return "f7";
    case Qt::Key_F8:
        return "f8";
    case Qt::Key_F9:
        return "f9";
    case Qt::Key_F10:
        return "f10";
    case Qt::Key_F11:
        return "f11";
    case Qt::Key_F12:
        return "f12";
    case Qt::Key_Print:
        return "print";
    case Qt::Key_Pause:
        return "pause";
    default:
        break;
    }
    const QString text = event->text();
    if (text.size() == 1) {
        const QChar ch = text.at(0);
        if (ch.isLetterOrNumber()) {
            return ch.toLower();
        }
        switch (ch.unicode()) {
        case '-': return "minus";
        case '=': return "equal";
        case '[': return "bracket_left";
        case ']': return "bracket_right";
        case ';': return "semicolon";
        case '\'': return "apostrophe";
        case ',': return "comma";
        case '.': return "dot";
        case '/': return "slash";
        case '\\': return "backslash";
        case '`': return "grave_accent";
        default: break;
        }
    }
    return {};
}

} // namespace

VirtualMachineWindow::VirtualMachineWindow(VmConfig config, QWidget* parent)
    : QWidget(parent),
      config_(std::move(config)) {
    buildUi();
    qApp->installEventFilter(this);
    macMouseDeltaTracker_ = new MacMouseDeltaTracker();
    qApp->installNativeEventFilter(macMouseDeltaTracker_);
}

VirtualMachineWindow::~VirtualMachineWindow() {
    if (macMouseDeltaTracker_) {
        qApp->removeNativeEventFilter(macMouseDeltaTracker_);
        delete macMouseDeltaTracker_;
        macMouseDeltaTracker_ = nullptr;
    }
    qApp->removeEventFilter(this);
    stopVm();
}

void VirtualMachineWindow::buildUi() {
    setWindowTitle(vmTitle(config_));
    resize(1280, 820);
    if (QScreen* screen = QApplication::primaryScreen()) {
        const QRect available = screen->availableGeometry();
        resize(qBound(1120, int(available.width() * 0.92), available.width()),
               qBound(760, int(available.height() * 0.90), available.height()));
    }
    setStyleSheet("QWidget{background:#111827;color:#e5e7eb;font-size:14px;}"
                  "QLabel#display{background:#05070a;border:1px solid #253044;border-radius:8px;}"
                  "QPushButton{border-radius:10px;background:#1f2937;color:#f9fafb;border:1px solid #374151;font-weight:700;}"
                  "QPushButton:hover{background:#2b3648;}"
                  "QPushButton#danger:hover{background:#7f1d1d;border-color:#991b1b;}"
                  "QPushButton#active{background:#2563eb;border-color:#60a5fa;}");

    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(8, 8, 8, 8);
    root->setSpacing(6);

    auto* top = new QHBoxLayout();
    top->setSpacing(8);
    titleLabel_ = new QLabel(windowTitle());
    titleLabel_->setStyleSheet("font-weight:700;font-size:16px;");
    statusLabel_ = new QLabel("正在启动...");
    statusLabel_->setStyleSheet("color:#9ca3af;");

    powerButton_ = makeToolButton(QStringLiteral("⏻"), QStringLiteral("关闭虚拟机"));
    powerButton_->setObjectName("danger");
    rebootButton_ = makeToolButton(QStringLiteral("↻"), QStringLiteral("重启虚拟机"));
    imageButton_ = makeToolButton(QStringLiteral("ISO"), QStringLiteral("弹出或更换安装镜像"));
    if (isSystemSoftwareConfig(config_)) {
        imageButton_->setToolTip("更换 Floppy A/B（会重启运行时）");
    }
    fullscreenButton_ = makeToolButton(QStringLiteral("⛶"), QStringLiteral("全屏"));
    mouseLockButton_ = makeToolButton(QStringLiteral("⌖"), QStringLiteral("锁定鼠标"));

    connect(powerButton_, &QPushButton::clicked, this, [this] { togglePower(); });
    connect(rebootButton_, &QPushButton::clicked, this, [this] { rebootVm(); });
    connect(imageButton_, &QPushButton::clicked, this, [this] { openImageMenu(); });
    connect(fullscreenButton_, &QPushButton::clicked, this, [this] { toggleFullscreenMode(); });
    connect(mouseLockButton_, &QPushButton::clicked, this, [this] { toggleMouseLock(); });

    top->addWidget(titleLabel_);
    top->addStretch(1);
    top->addWidget(powerButton_);
    top->addWidget(rebootButton_);
    top->addWidget(imageButton_);
    top->addWidget(fullscreenButton_);
    top->addWidget(mouseLockButton_);
    top->addWidget(statusLabel_);
    root->addLayout(top);

    displayLabel_ = new QLabel("VirtualWorld 正在接管虚拟机画面...");
    setFocusPolicy(Qt::StrongFocus);
    setMouseTracking(true);
    displayLabel_->setFocusPolicy(Qt::NoFocus);
    displayLabel_->setMouseTracking(true);
    displayLabel_->installEventFilter(this);
    displayLabel_->setObjectName("display");
    displayLabel_->setAlignment(Qt::AlignCenter);
    displayLabel_->setWordWrap(true);
    const QString arch = normalizeArchitecture(config_.architecture);
    const bool lowPowerGuest = arch == "ppc" || arch == "ppc64" || arch == "m68k";
    displayLabel_->setMinimumSize(lowPowerGuest ? QSize(880, 560) : QSize(1120, 700));
    displayLabel_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    displayLabel_->setScaledContents(false);
    root->addWidget(displayLabel_, 1);

    frameTimer_ = new QTimer(this);
    frameTimer_->setInterval(frameIntervalMs());
    connect(frameTimer_, &QTimer::timeout, this, [this] { captureFrame(); });
    mouseMoveTimer_ = new QTimer(this);
    mouseMoveTimer_->setInterval(16);
    mouseMoveTimer_->setTimerType(Qt::PreciseTimer);
    connect(mouseMoveTimer_, &QTimer::timeout, this, [this] { flushMouseMove(); });
    responsiveFrameTimer_ = new QTimer(this);
    responsiveFrameTimer_->setSingleShot(true);
    connect(responsiveFrameTimer_, &QTimer::timeout, this, [this] {
        if (frameTimer_) {
            frameTimer_->setInterval(frameIntervalMs());
        }
    });
    qmpSocket_ = new QLocalSocket(this);
    connect(qmpSocket_, &QLocalSocket::readyRead, this, [this] { processQmpData(); });
    connect(qmpSocket_, &QLocalSocket::disconnected, this, [this] {
        qmpCapabilitiesReady_ = false;
    });
    qmpConnectTimer_ = new QTimer(this);
    qmpConnectTimer_->setInterval(50);
    connect(qmpConnectTimer_, &QTimer::timeout, this, [this] { connectQmp(); });
    connect(&process_, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int exitCode, QProcess::ExitStatus status) {
                frameTimer_->stop();
                frameCapturePending_ = false;
                closeQmp();
                const QString stderrText = QString::fromUtf8(process_.readAllStandardError()).trimmed();
                if (isSystemSoftwareConfig(config_)) {
                    statusLabel_->setText(QString("已停止 (%1)").arg(exitCode));
                    if (status != QProcess::NormalExit || exitCode != 0) {
                        const QString message = QString("System Software 运行时已退出（代码 %1）。%2")
                                                    .arg(exitCode)
                                                    .arg(stderrText.isEmpty() ? QString() : "\n\n运行时输出：\n" + stderrText.left(1200));
                        displayLabel_->setText(message);
                        if (!userRequestedStop_) emit bootFailed(message);
                    }
                    if (!systemSoftwareRestartPending_) QTimer::singleShot(0, this, &QObject::deleteLater);
                    return;
                }
                const QString stderrLower = stderrText.toLower();
                const bool accelerationFailure = stderrLower.contains("hv_no_device")
                                                 || stderrLower.contains("vgic")
                                                 || stderrLower.contains("hypervisor")
                                                 || stderrLower.contains("accelerator")
                                                 || stderrLower.contains("accel=");
                if (!retriedTcgAfterAccelerationFailure_ && accelerationFailure && config_.accelerator.toLower() != "tcg") {
                    retriedTcgAfterAccelerationFailure_ = true;
                    config_.accelerator = "tcg";
                    statusLabel_->setText("硬件加速不可用，正在改用 TCG...");
                    displayLabel_->setText("当前硬件加速后端启动失败，VirtualWorld 正在自动改用 TCG 软件模拟。\n"
                                           "这会慢一些，但更稳定。");
                    QTimer::singleShot(300, this, [this] {
                        QString retryError;
                        if (!start(&retryError)) {
                            statusLabel_->setText("已停止");
                            displayLabel_->setText("改用 TCG 后仍无法启动：\n" + retryError);
                        }
                    });
                    return;
                }
                statusLabel_->setText(QString("已停止 (%1)").arg(exitCode));
                if (status != QProcess::NormalExit || exitCode != 0) {
                    QString message = QString("虚拟机启动失败或已退出（代码 %1）。\n请检查架构、固件、镜像和 QEMU 后端是否匹配。").arg(exitCode);
                    if (exitCode == 9 || exitCode == 137) {
                        message += "\n\n这个退出码通常表示 QEMU 被系统终止，常见原因是包内 QEMU/依赖不可用、架构/加速后端不兼容，或镜像启动链不完整。";
                    }
                    const QemuEngineInfo runtime = engine_.detect(config_.architecture);
                    if (runtime.systemEmulator.available) {
                        message += QString("\n\n当前 QEMU：%1（%2）").arg(runtime.systemEmulator.executable, runtime.systemEmulator.source);
                    }
                    if (!stderrText.isEmpty()) {
                        message += "\n\nQEMU 输出：\n" + stderrText.left(1200);
                    }
                    displayLabel_->setText(message);
                    if (!userRequestedStop_ && !firstFrameShown_) {
                        emit bootFailed(message);
                    }
                }
            });
}

void VirtualMachineWindow::resizeEvent(QResizeEvent* event) {
    QWidget::resizeEvent(event);
    rescaleLastFrame();
}

void VirtualMachineWindow::keyPressEvent(QKeyEvent* event) {
    if (handleKeyEvent(event, true)) {
        event->accept();
        return;
    }
    QWidget::keyPressEvent(event);
}

void VirtualMachineWindow::keyReleaseEvent(QKeyEvent* event) {
    if (handleKeyEvent(event, false)) {
        event->accept();
        return;
    }
    QWidget::keyReleaseEvent(event);
}

bool VirtualMachineWindow::eventFilter(QObject* watched, QEvent* event) {
    if (event->type() == QEvent::ApplicationDeactivate && mouseLocked_) {
        releaseInputCapture();
    }

    if (event->type() == QEvent::Show && mouseLocked_) {
        auto* widget = qobject_cast<QWidget*>(watched);
        if (widget && widget != this && (widget->isModal() || qobject_cast<QDialog*>(widget))) {
            releaseInputCapture();
        }
    }

    if (mouseLocked_
        && watched != this
        && (event->type() == QEvent::KeyPress || event->type() == QEvent::KeyRelease)) {
        auto* widget = qobject_cast<QWidget*>(watched);
        if (widget && widget->window() == window()) {
            auto* keyEvent = static_cast<QKeyEvent*>(event);
            if (handleKeyEvent(keyEvent, event->type() == QEvent::KeyPress)) {
                keyEvent->accept();
                return true;
            }
        }
    }

    if (watched == displayLabel_) {
        if (event->type() == QEvent::MouseButtonPress) {
            auto* mouseEvent = static_cast<QMouseEvent*>(event);
            setFocus(Qt::MouseFocusReason);
            const QPoint globalPosition = globalMousePosition(mouseEvent);
            hasMousePosition_ = true;
            lastMousePosition_ = globalPosition;
            if (absoluteTabletActive_) {
                forwardMouseMove(globalPosition);
            }
            if (forwardMouseButton(mouseEvent->button(), true)) {
                mouseEvent->accept();
                return true;
            }
        } else if (event->type() == QEvent::MouseButtonRelease) {
            auto* mouseEvent = static_cast<QMouseEvent*>(event);
            if (forwardMouseButton(mouseEvent->button(), false)) {
                mouseEvent->accept();
                return true;
            }
        } else if (event->type() == QEvent::MouseMove) {
            auto* mouseEvent = static_cast<QMouseEvent*>(event);
            if (forwardMouseMove(globalMousePosition(mouseEvent))) {
                mouseEvent->accept();
                return true;
            }
        } else if (event->type() == QEvent::Wheel) {
            auto* wheelEvent = static_cast<QWheelEvent*>(event);
            if (forwardMouseWheel(wheelEvent->angleDelta())) {
                wheelEvent->accept();
                return true;
            }
        }
    }
    return QWidget::eventFilter(watched, event);
}

bool VirtualMachineWindow::handleKeyEvent(QKeyEvent* event, bool pressed) {
    if (pressed
        && mouseLocked_
        && event->modifiers().testFlag(Qt::ControlModifier)
        && event->modifiers().testFlag(Qt::AltModifier)
        && event->key() == Qt::Key_G) {
        // Cmd/Ctrl+V must remain available to the guest. Use one unambiguous
        // release shortcut instead of consuming regular editing shortcuts.
        releaseInputCapture();
        return true;
    }
    return forwardKeyToQemu(event, pressed);
}

namespace {

// Best-effort Qt::Key -> Mac keycode (Mini vMac / ADB convention). Returns 0 for
// unmapped keys. This is a small subset; a full table would cover every key.
// It cannot be verified without a booting guest, which the in-process engine does
// not run (synthetic ROM, no Mac OS), so it is wired but not claimed-correct.
uint8_t qtKeyToMacKeyCode(int key) {
    switch (key) {
    case Qt::Key_A: return 0x00;
    case Qt::Key_S: return 0x01;
    case Qt::Key_D: return 0x02;
    case Qt::Key_F: return 0x03;
    case Qt::Key_H: return 0x04;
    case Qt::Key_G: return 0x05;
    case Qt::Key_Z: return 0x06;
    case Qt::Key_X: return 0x07;
    case Qt::Key_C: return 0x08;
    case Qt::Key_V: return 0x09;
    case Qt::Key_B: return 0x0B;
    case Qt::Key_Q: return 0x0C;
    case Qt::Key_W: return 0x0D;
    case Qt::Key_E: return 0x0E;
    case Qt::Key_R: return 0x0F;
    case Qt::Key_Y: return 0x10;
    case Qt::Key_T: return 0x11;
    case Qt::Key_1: return 0x12;
    case Qt::Key_2: return 0x13;
    case Qt::Key_3: return 0x14;
    case Qt::Key_4: return 0x15;
    case Qt::Key_6: return 0x16;
    case Qt::Key_5: return 0x17;
    case Qt::Key_Equal: return 0x18;
    case Qt::Key_9: return 0x19;
    case Qt::Key_7: return 0x1A;
    case Qt::Key_Minus: return 0x1B;
    case Qt::Key_8: return 0x1C;
    case Qt::Key_0: return 0x1D;
    case Qt::Key_O: return 0x1F;
    case Qt::Key_U: return 0x20;
    case Qt::Key_BracketLeft: return 0x21;
    case Qt::Key_I: return 0x22;
    case Qt::Key_P: return 0x23;
    case Qt::Key_Return: return 0x24;
    case Qt::Key_L: return 0x25;
    case Qt::Key_J: return 0x26;
    case Qt::Key_Apostrophe: return 0x27;
    case Qt::Key_K: return 0x28;
    case Qt::Key_Semicolon: return 0x29;
    case Qt::Key_Backslash: return 0x2A;
    case Qt::Key_Comma: return 0x2B;
    case Qt::Key_Slash: return 0x2C;
    case Qt::Key_N: return 0x2D;
    case Qt::Key_M: return 0x2E;
    case Qt::Key_Period: return 0x2F;
    case Qt::Key_Tab: return 0x30;
    case Qt::Key_Space: return 0x31;
    case Qt::Key_QuoteLeft: return 0x32;
    case Qt::Key_Backspace: return 0x33;
    case Qt::Key_Escape: return 0x35;
    case Qt::Key_Left: return 0x7B;
    case Qt::Key_Right: return 0x7C;
    case Qt::Key_Down: return 0x7D;
    case Qt::Key_Up: return 0x7E;
    case Qt::Key_Delete: return 0x75;
    default: return 0;
    }
}

} // namespace

bool VirtualMachineWindow::forwardKeyToQemu(QKeyEvent* event, bool pressed) {
    if (isSystemSoftwareConfig(config_) && systemSoftwareEngine_.isRunning()) {
        const uint8_t code = qtKeyToMacKeyCode(event->key());
        if (code != 0) systemSoftwareEngine_.setKey(code, pressed);
        return true;
    }
    if (!mouseLocked_ || process_.state() == QProcess::NotRunning) {
        return false;
    }
    if (!qmpCapabilitiesReady_) {
        return true;
    }
    const QString key = qemuKeyName(event);
    if (key.isEmpty()) {
        return true;
    }
    if (pressed) {
        if (event->isAutoRepeat() || pressedKeys_.contains(key)) {
            return true;
        }
        pressedKeys_.insert(key);
    } else if (!pressedKeys_.remove(key)) {
        return true;
    }
    return sendQmpKeyEvent(key, pressed);
}

void VirtualMachineWindow::mousePressEvent(QMouseEvent* event) {
    setFocus(Qt::MouseFocusReason);
    if (absoluteTabletActive_) {
        forwardMouseMove(globalMousePosition(event));
    }
    if (mouseLocked_ && forwardMouseButton(event->button(), true)) {
        event->accept();
        return;
    }
    QWidget::mousePressEvent(event);
}

void VirtualMachineWindow::mouseReleaseEvent(QMouseEvent* event) {
    if (mouseLocked_ && forwardMouseButton(event->button(), false)) {
        event->accept();
        return;
    }
    QWidget::mouseReleaseEvent(event);
}

void VirtualMachineWindow::mouseMoveEvent(QMouseEvent* event) {
    if (mouseLocked_ && forwardMouseMove(globalMousePosition(event))) {
        event->accept();
        return;
    }
    QWidget::mouseMoveEvent(event);
}

void VirtualMachineWindow::wheelEvent(QWheelEvent* event) {
    if (mouseLocked_ && forwardMouseWheel(event->angleDelta())) {
        event->accept();
        return;
    }
    QWidget::wheelEvent(event);
}

bool VirtualMachineWindow::forwardMouseButton(Qt::MouseButton button, bool pressed) {
    int mask = 0;
    switch (button) {
    case Qt::LeftButton:
        mask = 1;
        break;
    case Qt::RightButton:
        mask = 2;
        break;
    case Qt::MiddleButton:
        mask = 4;
        break;
    default:
        return false;
    }

    if (pressed) {
        mouseButtonState_ |= mask;
    } else {
        mouseButtonState_ &= ~mask;
    }

    if (isSystemSoftwareConfig(config_) && systemSoftwareEngine_.isRunning()) {
        // Classic compact Macs expose one mouse button. Aggregate the host's
        // left/right/middle state so releasing one button cannot release the
        // guest button while another host button is still held.
        systemSoftwareEngine_.setMouseButton(mouseButtonState_ != 0);
        return true;
    }
    if (!mouseLocked_ || process_.state() == QProcess::NotRunning) {
        return false;
    }
    flushMouseMove();
    if (!qmpCapabilitiesReady_) {
        return true;
    }
    return sendAbsoluteMouseButton(button, pressed);
}

bool VirtualMachineWindow::forwardMouseMove(const QPoint& globalPosition) {
    if (isSystemSoftwareConfig(config_) && systemSoftwareEngine_.isRunning()) {
        const QPoint local = displayLabel_ ? displayLabel_->mapFromGlobal(globalPosition) : globalPosition;
        const QSize size = displayLabel_ ? displayLabel_->size() : QSize(512, 342);
        const int mx = size.width() > 0 ? qBound(0, int(local.x() * 512.0 / size.width()), 511) : 0;
        const int my = size.height() > 0 ? qBound(0, int(local.y() * 342.0 / size.height()), 341) : 0;
        systemSoftwareEngine_.setMouse(mx, my);
        return true;
    }
    if (!mouseLocked_ || process_.state() == QProcess::NotRunning) {
        return false;
    }
    if (absoluteTabletActive_) {
        QPoint position;
        if (!mapAbsoluteMousePosition(globalPosition, &position)) {
            return true;
        }
        pendingAbsoluteMousePosition_ = position;
        hasPendingAbsoluteMousePosition_ = true;
        if (mouseMoveTimer_ && !mouseMoveTimer_->isActive()) {
            mouseMoveTimer_->start();
        }
        requestResponsiveFrames();
        return true;
    }
    if (!qmpCapabilitiesReady_) {
        if (macMouseDeltaTracker_) {
            macMouseDeltaTracker_->reset();
        }
        lastMousePosition_ = globalPosition;
        hasMousePosition_ = true;
        pendingMouseDelta_ = {};
        return true;
    }
    QPoint nativeDelta;
    const bool hasNativeDelta = takeNativeMouseDelta(&nativeDelta);
    if (!hasMousePosition_) {
        lastMousePosition_ = globalPosition;
        hasMousePosition_ = true;
        return true;
    }
    const QPoint delta = hasNativeDelta ? nativeDelta : globalPosition - lastMousePosition_;
    lastMousePosition_ = globalPosition;
    if (delta.isNull()) {
        return true;
    }
    // QEMU's Cocoa frontend forwards native relative deltas directly. Scaling
    // them by the displayed screenshot size makes the guest pointer drift.
    pendingMouseDelta_ += delta;
    if (mouseMoveTimer_ && !mouseMoveTimer_->isActive()) {
        mouseMoveTimer_->start();
    }
    if (shouldRecenterMouse(globalPosition)) {
        recenterMouse();
    }
    requestResponsiveFrames();
    return true;
}

bool VirtualMachineWindow::takeNativeMouseDelta(QPoint* delta) {
    return macMouseDeltaTracker_ && macMouseDeltaTracker_->takeDelta(delta);
}

bool VirtualMachineWindow::forwardMouseWheel(const QPoint& angleDelta) {
    if (isSystemSoftwareConfig(config_) && systemSoftwareEngine_.isRunning()) {
        const int horizontal = (angleDelta.x() > 0) - (angleDelta.x() < 0);
        const int vertical = (angleDelta.y() > 0) - (angleDelta.y() < 0);
        systemSoftwareEngine_.setMouseWheel(horizontal, vertical);
        return true;
    }
    if (!mouseLocked_ || process_.state() == QProcess::NotRunning) {
        return false;
    }
    const int vertical = angleDelta.y() / 120;
    const int horizontal = angleDelta.x() / 120;
    if (vertical == 0 && horizontal == 0) {
        return true;
    }
    flushMouseMove();
    // QMP input-send-event for the bundled PPC backend accepts relative X/Y
    // axes but not Z. Keep wheel events on the monitor because they are rare.
    return sendMonitorCommand(QString("mouse_move %1 %2 %3\n")
                                  .arg(horizontal)
                                  .arg(0)
                                  .arg(vertical));
}

void VirtualMachineWindow::flushMouseMove() {
    if (absoluteTabletActive_) {
        if (!hasPendingAbsoluteMousePosition_) {
            if (mouseMoveTimer_) {
                mouseMoveTimer_->stop();
            }
            return;
        }
        if (!mouseLocked_ || process_.state() == QProcess::NotRunning || !qmpCapabilitiesReady_) {
            if (mouseMoveTimer_) {
                mouseMoveTimer_->stop();
            }
            return;
        }
        const QPoint position = pendingAbsoluteMousePosition_;
        hasPendingAbsoluteMousePosition_ = false;
        if (mouseMoveTimer_) {
            mouseMoveTimer_->stop();
        }
        sendAbsoluteMousePosition(position);
        return;
    }
    if (pendingMouseDelta_.isNull()) {
        if (mouseMoveTimer_) {
            mouseMoveTimer_->stop();
        }
        return;
    }
    const QPoint delta = pendingMouseDelta_;
    pendingMouseDelta_ = {};
    if (mouseMoveTimer_) {
        mouseMoveTimer_->stop();
    }
    if (!mouseLocked_ || process_.state() == QProcess::NotRunning || !qmpCapabilitiesReady_) {
        return;
    }
    sendRelativeMouseMove(delta);
}

bool VirtualMachineWindow::usesAbsoluteTablet() const {
    for (const QString& device : config_.usb.devices) {
        if (splitOptions(device).value(0).compare("usb-tablet", Qt::CaseInsensitive) == 0) {
            return true;
        }
    }
    return false;
}

bool VirtualMachineWindow::mapAbsoluteMousePosition(const QPoint& globalPosition, QPoint* position) const {
    if (!position || !displayLabel_
        || displayedFrameSize_.width() <= 0
        || displayedFrameSize_.height() <= 0) {
        return false;
    }
    const QPoint localPosition = displayLabel_->mapFromGlobal(globalPosition);
    const QRect frameRect(displayedFrameOrigin_, displayedFrameSize_);
    if (!frameRect.contains(localPosition)) {
        return false;
    }
    const int x = qBound(0, localPosition.x() - frameRect.left(), displayedFrameSize_.width() - 1);
    const int y = qBound(0, localPosition.y() - frameRect.top(), displayedFrameSize_.height() - 1);
    position->setX(qRound(double(x) * 0x7fff / qMax(1, displayedFrameSize_.width() - 1)));
    position->setY(qRound(double(y) * 0x7fff / qMax(1, displayedFrameSize_.height() - 1)));
    return true;
}

bool VirtualMachineWindow::sendAbsoluteMousePosition(const QPoint& position) {
    QJsonArray events;
    QJsonObject xData;
    xData.insert("axis", "x");
    xData.insert("value", position.x());
    QJsonObject xEvent;
    xEvent.insert("type", "abs");
    xEvent.insert("data", xData);
    events.append(xEvent);
    QJsonObject yData;
    yData.insert("axis", "y");
    yData.insert("value", position.y());
    QJsonObject yEvent;
    yEvent.insert("type", "abs");
    yEvent.insert("data", yData);
    events.append(yEvent);
    return sendQmpInputEvents(events);
}

bool VirtualMachineWindow::sendAbsoluteMouseButton(Qt::MouseButton button, bool pressed) {
    QString buttonName;
    switch (button) {
    case Qt::LeftButton:
        buttonName = "left";
        break;
    case Qt::RightButton:
        buttonName = "right";
        break;
    case Qt::MiddleButton:
        buttonName = "middle";
        break;
    default:
        return false;
    }
    QJsonObject data;
    data.insert("button", buttonName);
    data.insert("down", pressed);
    QJsonObject event;
    event.insert("type", "btn");
    event.insert("data", data);
    QJsonArray events;
    events.append(event);
    return sendQmpInputEvents(events);
}

bool VirtualMachineWindow::sendAbsoluteMouseWheel(int steps) {
    if (steps == 0) {
        return true;
    }
    QJsonObject data;
    data.insert("axis", "z");
    data.insert("value", steps);
    QJsonObject event;
    event.insert("type", "rel");
    event.insert("data", data);
    QJsonArray events;
    events.append(event);
    return sendQmpInputEvents(events);
}

bool VirtualMachineWindow::sendRelativeMouseMove(const QPoint& delta) {
    if (delta.isNull()) {
        return true;
    }

    QJsonArray events;
    auto appendAxis = [&events](const QString& axis, int value) {
        if (value == 0) {
            return;
        }
        QJsonObject data;
        data.insert("axis", axis);
        data.insert("value", value);
        QJsonObject event;
        event.insert("type", "rel");
        event.insert("data", data);
        events.append(event);
    };
    appendAxis("x", delta.x());
    appendAxis("y", delta.y());
    return sendQmpInputEvents(events);
}

bool VirtualMachineWindow::sendQmpKeyEvent(const QString& key, bool pressed) {
    QJsonArray events;
    QJsonObject qcodeData;
    qcodeData.insert("type", "qcode");
    qcodeData.insert("data", key);
    QJsonObject data;
    data.insert("key", qcodeData);
    data.insert("down", pressed);
    QJsonObject inputEvent;
    inputEvent.insert("type", "key");
    inputEvent.insert("data", data);
    events.append(inputEvent);
    return sendQmpInputEvents(events);
}

bool VirtualMachineWindow::sendQmpInputEvents(const QJsonArray& events) {
    if (events.isEmpty()) {
        return true;
    }
    QJsonObject arguments;
    arguments.insert("events", events);
    QJsonObject command;
    command.insert("execute", "input-send-event");
    command.insert("arguments", arguments);
    return sendQmpCommand(QJsonDocument(command).toJson(QJsonDocument::Compact));
}

bool VirtualMachineWindow::sendQmpScreenDump(const QString& path) {
    QJsonObject arguments;
    arguments.insert("filename", path);
    QJsonObject command;
    command.insert("execute", "screendump");
    command.insert("arguments", arguments);
    return sendQmpCommand(QJsonDocument(command).toJson(QJsonDocument::Compact));
}

bool VirtualMachineWindow::sendQmpCommand(const QByteArray& command) {
    if (!qmpSocket_ || !qmpCapabilitiesReady_
        || qmpSocket_->state() != QLocalSocket::ConnectedState) {
        return false;
    }
    QByteArray line = command;
    if (!line.endsWith('\n')) {
        line.append('\n');
    }
    return qmpSocket_->write(line) == line.size();
}

void VirtualMachineWindow::connectQmp() {
    if (process_.state() == QProcess::NotRunning || !qmpSocket_) {
        return;
    }
    if (qmpCapabilitiesReady_) {
        if (qmpConnectTimer_) {
            qmpConnectTimer_->stop();
        }
        return;
    }
    if (qmpSocket_->state() == QLocalSocket::ConnectedState
        || qmpSocket_->state() == QLocalSocket::ConnectingState
        || qmpSocketPath_.isEmpty()) {
        return;
    }
    qmpSocket_->abort();
    qmpSocket_->connectToServer(qmpSocketPath_);
}

void VirtualMachineWindow::processQmpData() {
    if (!qmpSocket_) {
        return;
    }
    qmpBuffer_.append(qmpSocket_->readAll());
    while (true) {
        const int newline = qmpBuffer_.indexOf('\n');
        if (newline < 0) {
            break;
        }
        const QByteArray line = qmpBuffer_.left(newline).trimmed();
        qmpBuffer_.remove(0, newline + 1);
        if (line.isEmpty()) {
            continue;
        }
        const QJsonDocument document = QJsonDocument::fromJson(line);
        if (!document.isObject()) {
            continue;
        }
        const QJsonObject response = document.object();
        if (response.contains("QMP")) {
            qmpGreetingReceived_ = true;
            if (!qmpCapabilitiesRequested_) {
                qmpCapabilitiesRequested_ = true;
                if (qmpSocket_->write("{\"execute\":\"qmp_capabilities\"}\n") < 0) {
                    qmpCapabilitiesRequested_ = false;
                }
            }
            continue;
        }
        if (qmpGreetingReceived_ && qmpCapabilitiesRequested_ && response.contains("return")) {
            qmpCapabilitiesReady_ = true;
            if (qmpConnectTimer_) {
                qmpConnectTimer_->stop();
            }
            if (mouseLocked_) {
                statusLabel_->setText(absoluteTabletActive_ ? "鼠标绝对定位已锁定"
                                                            : "鼠标与键盘已锁定");
                flushMouseMove();
            }
        }
    }
}

void VirtualMachineWindow::closeQmp() {
    if (qmpConnectTimer_) {
        qmpConnectTimer_->stop();
    }
    if (qmpSocket_) {
        qmpSocket_->abort();
    }
    qmpBuffer_.clear();
    qmpGreetingReceived_ = false;
    qmpCapabilitiesRequested_ = false;
    qmpCapabilitiesReady_ = false;
    if (!qmpSocketPath_.isEmpty()) {
        QFile::remove(qmpSocketPath_);
        qmpSocketPath_.clear();
    }
}

void VirtualMachineWindow::requestResponsiveFrames() {
    if (!frameTimer_ || process_.state() == QProcess::NotRunning) {
        return;
    }
    const QString arch = normalizeArchitecture(config_.architecture);
    // QMP serializes screendump and input commands. Capturing a frame for
    // every mouse packet starves PPC input on TCG and makes the cursor lag.
    const int responsiveInterval = (arch == "ppc" || arch == "ppc64" || arch == "m68k") ? 260 : 140;
    if (frameTimer_->interval() != responsiveInterval) {
        frameTimer_->setInterval(responsiveInterval);
    }
    if (responsiveFrameTimer_) {
        responsiveFrameTimer_->start(550);
    }
}

bool VirtualMachineWindow::shouldRecenterMouse(const QPoint& globalPosition) const {
    const QScreen* screen = QGuiApplication::screenAt(globalPosition);
    if (!screen) {
        return false;
    }
    const QRect geometry = screen->geometry();
    constexpr int edgeMargin = 2;
    return globalPosition.x() <= geometry.left() + edgeMargin
           || globalPosition.x() >= geometry.right() - edgeMargin
           || globalPosition.y() <= geometry.top() + edgeMargin
           || globalPosition.y() >= geometry.bottom() - edgeMargin;
}

void VirtualMachineWindow::recenterMouse() {
    if (mouseAnchor_.isNull()) {
        return;
    }
    // Qt reports a synthetic move after QCursor::setPos(). Treat the next
    // event only as a new baseline so the warp can never reach the guest.
    hasMousePosition_ = false;
    if (macMouseDeltaTracker_) {
        macMouseDeltaTracker_->reset();
    }
    QCursor::setPos(mouseAnchor_);
}

QString VirtualMachineWindow::framePath() const {
    return QDir(QStandardPaths::writableLocation(QStandardPaths::TempLocation))
        .filePath(QString("virtualworld-%1.ppm").arg(reinterpret_cast<quintptr>(this)));
}

bool VirtualMachineWindow::start(QString* error) {
    closeQmp();
    QFile::remove(framePath());
    userRequestedStop_ = false;
    captureAttempts_ = 0;
    firstFrameShown_ = false;
    frameCapturePending_ = false;
    absoluteTabletActive_ = usesAbsoluteTablet();
    if (isSystemSoftwareConfig(config_)) {
        // In-process engine: no child process, no PID. The four prebuilt
        // minivmac-* executables are no longer launched.
        // Real frames (512x342, 1-bit) are delivered on the engine worker thread
        // via the frame callback; we marshal them to the GUI thread and paint.
        // The synthetic ROM is not a Macintosh ROM and will not boot Mac OS, but
        // the frame data is genuine.
        systemSoftwareEngine_.setFrameCallback([this](const vw::MvFrame& frame) {
            paintInProcessFrame(frame);
        });
        if (!systemSoftwareEngine_.start(config_, error)) {
            return false;
        }
        statusLabel_->setText(QString("System Software %1（进程内引擎）运行中")
                                  .arg(config_.systemSoftwareVersion));
        displayLabel_->setText("System Software 进程内引擎已启动（create / loadRom / attachDisk / start 已执行）。\n"
                               "帧画面已接通：引擎每帧回调整帧缓冲（512x342, 1-bit），窗口实时显示。\n"
                               "输入已路由到引擎 setKey/setMouse；鼠标按钮/滚轮接口待补，暂不端到端验证。\n"
                               "注意：合成 ROM 非 Macintosh ROM，不会启动 Mac OS，但帧数据为真。");
        return true;
    }
    VmConfig launch = config_;
    launch.graphics.display = "none";
    const QString arch = normalizeArchitecture(launch.architecture);
    if (arch == "x86_64" || arch == "i386") {
        const QString guest = launch.guestOs.trimmed().toLower();
        if (guest.contains("old windows")) {
            launch.graphics.adapter.clear();
            launch.graphics.vga = "std";
        } else if (guest.contains("windows")) {
            launch.graphics.adapter.clear();
            launch.graphics.vga = "std";
        } else if (launch.graphics.adapter.trimmed().isEmpty()
                   && launch.graphics.vga.trimmed().isEmpty()) {
            launch.graphics.vga = "std";
        }
    } else if (arch == "aarch64" || arch == "arm" || arch == "riscv64") {
        const QString adapter = launch.graphics.adapter.trimmed().toLower();
        if (adapter.isEmpty() || adapter == "vga" || adapter == "std" || adapter == "cirrus") {
            launch.graphics.adapter = "virtio-gpu-pci";
            launch.graphics.vga.clear();
        }
    } else if (arch == "ppc" || arch == "ppc64") {
        const QString adapter = launch.graphics.adapter.trimmed().toLower();
        if (adapter.startsWith("virtio") || adapter.isEmpty()
            || adapter == "std" || adapter == "vga" || adapter == "cirrus"
            || adapter == "vmware" || adapter == "qxl" || adapter == "virtio") {
            launch.graphics.adapter.clear();
            launch.graphics.vga = adapter == "cirrus" ? "cirrus" : "std";
        }
    }
    launch.graphics.openGl = false;
    launch.graphics.autoResize = false;
    launch.graphics.dynamicResolution = false;
    launch.graphics.retina = false;
    launch.spice.enabled = false;
    launch.extraArgs.removeAll("-display");
    launch.extraArgs.removeAll("none");
    launch.extraArgs.removeAll("cocoa");
    launch.extraArgs.removeAll("gtk");
    launch.extraArgs.removeAll("sdl");
    launch.extraArgs.removeAll("headless");
    for (int i = launch.extraArgs.size() - 2; i >= 0; --i) {
        if (launch.extraArgs.at(i) == "-device") {
            const QString device = launch.extraArgs.at(i + 1).toLower();
            if (device.contains("virtio-vga") || device.contains("virtio-gpu")
                || device.contains("qxl") || device == "vga" || device == "ramfb") {
                launch.extraArgs.removeAt(i + 1);
                launch.extraArgs.removeAt(i);
            }
        }
    }
    if (!launch.graphics.vga.trimmed().isEmpty()) {
        launch.extraArgs.removeAll("-vga");
        launch.extraArgs.removeAll("virtio");
        launch.extraArgs.removeAll("std");
        launch.extraArgs.removeAll("cirrus");
    }
    QString tempDir = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    if (tempDir.isEmpty()) {
        tempDir = QDir::tempPath();
    }
    qmpSocketPath_ = QDir(tempDir).filePath(
        QString("vw-qmp-%1.sock").arg(reinterpret_cast<quintptr>(this)));
    QFile::remove(qmpSocketPath_);
    QStringList extraArgs = {"-monitor", "stdio",
                             "-qmp", "unix:" + qmpSocketPath_ + ",server=on,wait=off"};
    if (!engine_.prepareProcess(process_, launch, extraArgs, error)) {
        closeQmp();
        return false;
    }

    process_.start();
    if (!process_.waitForStarted(5000)) {
        closeQmp();
        if (error) {
            *error = process_.errorString();
        }
        return false;
    }
    if (qmpConnectTimer_) {
        qmpConnectTimer_->start();
        QTimer::singleShot(0, this, [this] { connectQmp(); });
    }
    statusLabel_->setText(QString("运行中 PID %1").arg(process_.processId()));
    displayLabel_->setText("QEMU 已启动，正在等待第一帧画面...");
    frameTimer_->setInterval(frameIntervalMs());
    QTimer::singleShot(250, this, [this] { captureFrame(); });
    QTimer::singleShot(900, this, [this] { captureFrame(); });
    QTimer::singleShot(1800, this, [this] { captureFrame(); });
    frameTimer_->start();
    return true;
}

qint64 VirtualMachineWindow::processId() const {
    // The in-process engine has no PID. Return 0 so callers that expect a process
    // handle (status text, external-window activation) get a stable sentinel.
    if (isSystemSoftwareConfig(config_)) return 0;
    return process_.processId();
}

bool VirtualMachineWindow::isRunning() const {
    if (isSystemSoftwareConfig(config_)) return systemSoftwareEngine_.isRunning();
    return process_.state() != QProcess::NotRunning;
}

bool VirtualMachineWindow::matchesConfig(const VmConfig& config) const {
    return isSystemSoftwareConfig(config_) && isSystemSoftwareConfig(config)
           && config_.storageDir == config.storageDir;
}

void VirtualMachineWindow::activateRuntimeWindow() {
    if (isSystemSoftwareConfig(config_)) {
        // 进程内引擎没有独立窗口可激活。
        return;
    }
    if (isRunning()) {
        activateExternalApplication(process_.processId());
    }
}

int VirtualMachineWindow::frameIntervalMs() const {
    const QString arch = normalizeArchitecture(config_.architecture);
    if (arch == "ppc" || arch == "ppc64" || arch == "m68k") {
        return firstFrameShown_ ? 650 : 500;
    }
    return firstFrameShown_ ? 500 : 400;
}

bool VirtualMachineWindow::sendMonitorCommand(const QString& command, QByteArray* reply) {
    if (process_.state() == QProcess::NotRunning) {
        return false;
    }
    if (process_.write(command.toUtf8()) < 0) {
        return false;
    }
    if (!reply) {
        return true;
    }
    if (!process_.waitForBytesWritten(500)) {
        return false;
    }
    process_.waitForReadyRead(800);
    if (reply) {
        *reply = process_.readAllStandardOutput();
        while (process_.waitForReadyRead(80)) {
            reply->append(process_.readAllStandardOutput());
        }
    } else {
        process_.readAllStandardOutput();
    }
    return true;
}

void VirtualMachineWindow::captureFrame() {
    // System Software runs in-process with no QMP/screendump channel; frames are
    // not yet wired through Screen_OutputFrame. Never poll a non-existent channel.
    if (isSystemSoftwareConfig(config_)) {
        return;
    }
    if (process_.state() == QProcess::NotRunning || frameCapturePending_) {
        return;
    }
    frameCapturePending_ = true;
    ++captureAttempts_;
    const QString path = framePath();
    QFile::remove(path);
    const bool sent = qmpCapabilitiesReady_
                          ? sendQmpScreenDump(path)
                          : sendMonitorCommand("screendump " + hmpQuote(path) + "\n");
    if (!sent) {
        frameCapturePending_ = false;
        statusLabel_->setText(captureAttempts_ < 8 ? "控制通道准备中..." : "等待第一帧...");
        if (!firstFrameShown_ && captureAttempts_ == 8) {
            displayLabel_->setText("QEMU 已启动，但 VirtualWorld 还没有收到画面。\n"
                                   "如果这里停留过久，请检查安装镜像、客户机架构和固件配置是否匹配。");
        }
        return;
    }
    const QString arch = normalizeArchitecture(config_.architecture);
    const bool responsiveInput = responsiveFrameTimer_ && responsiveFrameTimer_->isActive();
    const int readDelayMs = (arch == "ppc" || arch == "ppc64" || arch == "m68k")
                                ? (responsiveInput ? 75 : 140)
                                : (responsiveInput ? 45 : 80);
    QTimer::singleShot(readDelayMs, this, [this, path] {
        frameCapturePending_ = false;
        if (process_.state() != QProcess::NotRunning) {
            updateImage(path);
        }
    });
}

void VirtualMachineWindow::togglePower() {
    stopVm();
    close();
}

void VirtualMachineWindow::rebootVm() {
    if (!isRunning()) {
        return;
    }
    if (isSystemSoftwareConfig(config_)) {
        systemSoftwareRestartPending_ = true;
        stopVm();
        QTimer::singleShot(400, this, [this] {
            QString error;
            const bool started = start(&error);
            systemSoftwareRestartPending_ = false;
            if (!started) {
                displayLabel_->setText("System Software 重启失败：\n" + error);
                emit bootFailed(error);
                deleteLater();
            }
        });
        return;
    }
    if (sendMonitorCommand("system_reset\n")) {
        statusLabel_->setText("正在重启...");
        QTimer::singleShot(1200, this, [this] { captureFrame(); });
    } else {
        QMessageBox::warning(this, "VirtualWorld", "无法连接虚拟机控制通道，重启失败。");
    }
}

void VirtualMachineWindow::openImageMenu() {
    QMenu menu(this);
    if (isSystemSoftwareConfig(config_)) {
        QAction* replaceA = menu.addAction("更换 Floppy A...");
        QAction* ejectA = menu.addAction("弹出 Floppy A");
        QAction* replaceB = menu.addAction("更换 Floppy B...");
        QAction* ejectB = menu.addAction("弹出 Floppy B");
        QAction* selected = menu.exec(imageButton_->mapToGlobal(imageButton_->rect().bottomLeft()));
        if (selected == replaceA) QTimer::singleShot(0, this, [this] { replaceSystemSoftwareFloppy(0); });
        else if (selected == ejectA) QTimer::singleShot(0, this, [this] { ejectSystemSoftwareFloppy(0); });
        else if (selected == replaceB) QTimer::singleShot(0, this, [this] { replaceSystemSoftwareFloppy(1); });
        else if (selected == ejectB) QTimer::singleShot(0, this, [this] { ejectSystemSoftwareFloppy(1); });
        return;
    }
    QAction* replace = menu.addAction("挂载/更换安装镜像...");
    QAction* eject = menu.addAction("弹出安装镜像");
    QAction* selected = menu.exec(imageButton_->mapToGlobal(imageButton_->rect().bottomLeft()));
    if (selected == replace) {
        // Let the native macOS menu close before opening NSOpenPanel.
        QTimer::singleShot(0, this, [this] { replaceImage(); });
    } else if (selected == eject) {
        QTimer::singleShot(0, this, [this] { ejectImage(); });
    }
}

void VirtualMachineWindow::replaceSystemSoftwareFloppy(int slot) {
    const QString path = QFileDialog::getOpenFileName(this,
        slot == 0 ? "选择 Floppy A" : "选择 Floppy B", QDir::homePath(),
        "软盘镜像 (*.dsk *.ima *.img *.flp);;所有文件 (*)");
    if (path.isEmpty()) return;
    for (int i = config_.disks.size() - 1; i >= 0; --i) {
        if (config_.disks.at(i).role.compare(slot == 0 ? "floppy-a" : "floppy-b", Qt::CaseInsensitive) == 0) {
            config_.disks.removeAt(i);
        }
    }
    DiskConfig disk;
    disk.path = QFileInfo(path).absoluteFilePath();
    disk.interfaceName = "floppy";
    disk.format = QFileInfo(path).suffix().toLower();
    disk.media = "floppy";
    disk.role = slot == 0 ? "floppy-a" : "floppy-b";
    config_.disks.append(disk);
    emit configChanged(config_);
    if (isRunning()) {
        systemSoftwareRestartPending_ = true;
        stopVm();
        QTimer::singleShot(350, this, [this] {
            QString error;
            const bool started = start(&error);
            systemSoftwareRestartPending_ = false;
            if (!started) {
                displayLabel_->setText("软盘更换后重启失败：\n" + error);
                emit bootFailed(error);
                deleteLater();
            }
        });
    }
    statusLabel_->setText(slot == 0 ? "Floppy A 已更换" : "Floppy B 已更换");
}

void VirtualMachineWindow::ejectSystemSoftwareFloppy(int slot) {
    const QString role = slot == 0 ? "floppy-a" : "floppy-b";
    for (int i = config_.disks.size() - 1; i >= 0; --i) {
        if (config_.disks.at(i).role.compare(role, Qt::CaseInsensitive) == 0) config_.disks.removeAt(i);
    }
    emit configChanged(config_);
    if (isRunning()) {
        systemSoftwareRestartPending_ = true;
        stopVm();
        QTimer::singleShot(350, this, [this] {
            QString error;
            const bool started = start(&error);
            systemSoftwareRestartPending_ = false;
            if (!started) {
                displayLabel_->setText("弹出软盘后重启失败：\n" + error);
                emit bootFailed(error);
                deleteLater();
            }
        });
    }
    statusLabel_->setText(slot == 0 ? "Floppy A 已弹出" : "Floppy B 已弹出");
}

QString VirtualMachineWindow::findFirstCdromDevice(const QByteArray& infoBlock) const {
    const QString text = QString::fromUtf8(infoBlock);
    const QStringList lines = text.split('\n', Qt::SkipEmptyParts);
    QString fallback;
    const QRegularExpression cdromName(
        R"((^|[-_.])(?:cd|cdrom)(?:\d|[-_.]|$))",
        QRegularExpression::CaseInsensitiveOption);
    for (QString line : lines) {
        line = line.trimmed();
        const int colon = line.indexOf(':');
        if (colon <= 0) {
            continue;
        }
        QString device = line.left(colon).trimmed();
        const int blockAnnotation = device.indexOf(" (#");
        if (blockAnnotation >= 0) {
            device.truncate(blockAnnotation);
        }
        const QString lower = line.toLower();
        if (cdromName.match(device).hasMatch()
            || lower.contains("cdrom")
            || lower.contains("cd-rom")
            || lower.contains("removable=1")) {
            return device;
        }
        for (const DiskConfig& disk : config_.disks) {
            if (disk.media == "cdrom"
                && !disk.path.trimmed().isEmpty()
                && line.contains(QFileInfo(disk.path).fileName(), Qt::CaseInsensitive)) {
                return device;
            }
        }
        if (fallback.isEmpty() && (lower.contains(".iso") || lower.contains("media=cdrom"))) {
            fallback = device;
        }
    }
    return fallback;
}

void VirtualMachineWindow::ejectImage() {
    QByteArray info;
    if (!sendMonitorCommand("info block\n", &info)) {
        QMessageBox::warning(this, "VirtualWorld", "无法连接虚拟机控制通道。");
        return;
    }
    const QString device = findFirstCdromDevice(info);
    if (device.isEmpty()) {
        QMessageBox::information(this, "VirtualWorld", "没有找到可弹出的安装镜像设备。");
        return;
    }
    QByteArray reply;
    if (!sendMonitorCommand("eject -f " + hmpQuote(device) + "\n", &reply)
        || QString::fromUtf8(reply).contains("Error:", Qt::CaseInsensitive)) {
        const QString detail = monitorErrorSummary(reply);
        QMessageBox::warning(this,
                             "VirtualWorld",
                             "弹出安装镜像失败。"
                                 + (detail.isEmpty() ? QString() : "\n" + detail));
        return;
    }
    config_.cdroms.clear();
    for (int i = config_.disks.size() - 1; i >= 0; --i) {
        if (config_.disks.at(i).media == "cdrom") {
            config_.disks.removeAt(i);
        }
    }
    emit configChanged(config_);
    statusLabel_->setText("安装镜像已弹出");
}

void VirtualMachineWindow::replaceImage() {
    const QString path = QFileDialog::getOpenFileName(
        this,
        "选择安装镜像",
        QDir::homePath(),
        "Images (*.iso *.cdr *.dmg *.ipsw *.img *.ima *.flp *.dsk *.qcow2 *.raw *.hfv *.toast);;All Files (*)");
    if (path.isEmpty()) {
        return;
    }
    QByteArray info;
    if (!sendMonitorCommand("info block\n", &info)) {
        QMessageBox::warning(this, "VirtualWorld", "无法连接虚拟机控制通道。");
        return;
    }
    const QString device = findFirstCdromDevice(info);
    if (device.isEmpty()) {
        QMessageBox::information(this, "VirtualWorld", "没有找到可更换的安装镜像设备。请在虚拟机配置中加入 CD/DVD 设备。");
        return;
    }
    QByteArray reply;
    if (!sendMonitorCommand("change " + hmpQuote(device) + " " + hmpQuote(path) + "\n", &reply)
        || QString::fromUtf8(reply).contains("Error:", Qt::CaseInsensitive)) {
        const QString detail = monitorErrorSummary(reply);
        QMessageBox::warning(this,
                             "VirtualWorld",
                             "更换安装镜像失败。"
                                 + (detail.isEmpty() ? QString() : "\n" + detail));
        return;
    }
    for (int i = config_.disks.size() - 1; i >= 0; --i) {
        if (config_.disks.at(i).media == "cdrom") {
            config_.disks.removeAt(i);
        }
    }
    config_.cdroms.clear();
    const QString suffix = QFileInfo(path).suffix().toLower();
    config_.disks.prepend(DiskConfig{path, "ide", suffix, true, "cdrom"});
    emit configChanged(config_);
    statusLabel_->setText("安装镜像已更换");
}

void VirtualMachineWindow::toggleFullscreenMode() {
    if (isFullScreen()) {
        showNormal();
        fullscreenButton_->setText(QStringLiteral("⛶"));
        fullscreenButton_->setToolTip("全屏");
    } else {
        showFullScreen();
        fullscreenButton_->setText(QStringLiteral("▣"));
        fullscreenButton_->setToolTip("退出全屏");
    }
}

void VirtualMachineWindow::toggleMouseLock() {
    if (mouseLocked_) {
        releaseInputCapture();
        return;
    }

    if (!suppressMouseLockHint_) {
        QMessageBox box(this);
        box.setWindowTitle("鼠标锁定");
        box.setText(absoluteTabletActive_
                        ? "鼠标会以绝对坐标发送给虚拟机，不会隐藏或移动宿主机指针。\n再次点击准星按钮即可退出。"
                        : "锁定后，鼠标和键盘会发送给虚拟机。\n按 Ctrl+Alt+G 可退出锁定。");
        QPushButton* ok = box.addButton("知道了", QMessageBox::AcceptRole);
        QPushButton* never = box.addButton("不再提示", QMessageBox::DestructiveRole);
        box.setDefaultButton(ok);
        box.exec();
        if (box.clickedButton() == never) {
            suppressMouseLockHint_ = true;
        }
    }

    if (process_.state() == QProcess::NotRunning) {
        return;
    }

    mouseAnchor_ = displayLabel_->mapToGlobal(displayLabel_->rect().center());
    lastMousePosition_ = mouseAnchor_;
    hasMousePosition_ = true;
    pendingMouseDelta_ = {};
    hasPendingAbsoluteMousePosition_ = false;
    if (!absoluteTabletActive_) {
        grabMouse();
        grabKeyboard();
        QApplication::setOverrideCursor(QCursor(Qt::BlankCursor));
        cursorOverrideActive_ = true;
        setCursor(Qt::BlankCursor);
        displayLabel_->setCursor(Qt::BlankCursor);
        // Ignore the synthetic move from the initial cursor warp.
        hasMousePosition_ = false;
        macMouseDeltaTracker_->setTarget(this);
        macMouseDeltaTracker_->setEnabled(true);
        QCursor::setPos(mouseAnchor_);
    } else {
        displayLabel_->setCursor(Qt::ArrowCursor);
    }
    mouseLocked_ = true;
    setFocus(Qt::OtherFocusReason);
    if (frameTimer_) {
        frameTimer_->setInterval(frameIntervalMs());
    }
    mouseLockButton_->setObjectName("active");
    mouseLockButton_->style()->unpolish(mouseLockButton_);
    mouseLockButton_->style()->polish(mouseLockButton_);
    statusLabel_->setText(qmpCapabilitiesReady_
                              ? (absoluteTabletActive_ ? "鼠标绝对定位已锁定"
                                                        : "鼠标与键盘已锁定")
                              : "正在连接输入通道...");
}

void VirtualMachineWindow::releaseInputCapture() {
    flushMouseMove();
    if (mouseMoveTimer_) {
        mouseMoveTimer_->stop();
    }
    if (mouseButtonState_ != 0) {
        if (isSystemSoftwareConfig(config_) && systemSoftwareEngine_.isRunning()) {
            systemSoftwareEngine_.setMouseButton(false);
        } else if (process_.state() != QProcess::NotRunning && qmpCapabilitiesReady_) {
            if (mouseButtonState_ & 1) sendAbsoluteMouseButton(Qt::LeftButton, false);
            if (mouseButtonState_ & 2) sendAbsoluteMouseButton(Qt::RightButton, false);
            if (mouseButtonState_ & 4) sendAbsoluteMouseButton(Qt::MiddleButton, false);
        }
    }
    releasePressedKeys();
    mouseButtonState_ = 0;
    if (mouseLocked_) {
        if (!absoluteTabletActive_) {
            releaseMouse();
            releaseKeyboard();
        }
        mouseLocked_ = false;
    }
    if (cursorOverrideActive_) {
        QApplication::restoreOverrideCursor();
        cursorOverrideActive_ = false;
    }
    unsetCursor();
    displayLabel_->unsetCursor();
    if (macMouseDeltaTracker_) {
        macMouseDeltaTracker_->setEnabled(false);
        macMouseDeltaTracker_->setTarget(nullptr);
    }
    mouseAnchor_ = {};
    hasMousePosition_ = false;
    pendingMouseDelta_ = {};
    hasPendingAbsoluteMousePosition_ = false;
    if (frameTimer_) {
        frameTimer_->setInterval(frameIntervalMs());
    }
    mouseLockButton_->setObjectName("");
    mouseLockButton_->style()->unpolish(mouseLockButton_);
    mouseLockButton_->style()->polish(mouseLockButton_);
    if (process_.state() != QProcess::NotRunning) {
        statusLabel_->setText(QString("运行中 PID %1").arg(process_.processId()));
    }
}

void VirtualMachineWindow::releasePressedKeys() {
    if (pressedKeys_.isEmpty()) {
        return;
    }
    const QSet<QString> keys = pressedKeys_;
    pressedKeys_.clear();
    if (!qmpCapabilitiesReady_ || process_.state() == QProcess::NotRunning) {
        return;
    }
    for (const QString& key : keys) {
        sendQmpKeyEvent(key, false);
    }
}

void VirtualMachineWindow::updateImage(const QString& path) {
    QImage image(path);
    if (image.isNull()) {
        if (!firstFrameShown_ && captureAttempts_ >= 10) {
            displayLabel_->setText("已经连接到虚拟机控制通道，但还没有生成有效画面。\n"
                                   "通常是启动镜像缺失、架构不匹配，或客户机仍在固件初始化。");
        }
        return;
    }
    if (!firstFrameShown_ && looksLikeUninitializedFrame(image)) {
        statusLabel_->setText("等待有效画面...");
        displayLabel_->setText(captureAttempts_ < 160
                                   ? "虚拟机显示设备已经启动，正在等待固件或安装器输出有效画面..."
                                   : "显示设备持续输出未初始化画面。请检查显卡类型、启动镜像、固件和客户机架构是否匹配。");
        return;
    }
    // PC firmware commonly renders SeaBIOS/iPXE as gray text on a black
    // framebuffer. That is a valid x86 display, not an inactive placeholder.
    if (!firstFrameShown_ && looksLikeInactiveDisplayPlaceholder(image)) {
        if (captureAttempts_ < 18) {
            statusLabel_->setText("等待客户机初始化显示...");
            displayLabel_->setText("虚拟机已启动，正在等待客户机初始化显示输出...");
            return;
        }
        // Do not hide QEMU's own diagnostic frame. It contains the most
        // useful state when a guest has not initialized a display surface.
        statusLabel_->setText("客户机尚未初始化显示");
    }
    const bool receivedFirstFrame = !firstFrameShown_;
    firstFrameShown_ = true;
    if (receivedFirstFrame) {
        emit firstFrameReady();
    }
    if (receivedFirstFrame && frameTimer_) {
        frameTimer_->setInterval(frameIntervalMs());
    }
    lastFrame_ = image;
    rescaleLastFrame();
}

void VirtualMachineWindow::rescaleLastFrame() {
    if (lastFrame_.isNull() || !displayLabel_) {
        return;
    }
    const QSize logicalTarget = displayLabel_->contentsRect().size();
    if (logicalTarget.width() <= 0 || logicalTarget.height() <= 0) {
        return;
    }
    const qreal dpr = qMax<qreal>(1.0, displayLabel_->devicePixelRatioF());
    const double scaleX = double(logicalTarget.width()) / double(qMax(1, lastFrame_.width()));
    const double scaleY = double(logicalTarget.height()) / double(qMax(1, lastFrame_.height()));
    double scale = qMin(scaleX, scaleY);
    if (lastFrame_.width() <= 1280 && lastFrame_.height() <= 800) {
        const double comfortableScale = qMin(scale, qMax(1.0, scale * 0.98));
        scale = comfortableScale;
    }
    const QString guest = config_.guestOs.trimmed().toLower();
    const QString arch = normalizeArchitecture(config_.architecture);
    const bool legacyGuest = guest.contains("dos")
                             || guest.contains("old windows")
                             || guest.contains("classic")
                             || arch == "ppc"
                             || arch == "ppc64"
                             || arch == "m68k";
    const bool integerFriendly = legacyGuest && lastFrame_.width() <= 1280 && lastFrame_.height() <= 800;
    if (legacyGuest) {
        scale = qMin(scale, 2.0);
    }
    if (integerFriendly && scale >= 2.0) {
        scale = qFloor(scale);
    }
    const QSize physicalScaledSize(qMax(1, int(lastFrame_.width() * scale * dpr)),
                                   qMax(1, int(lastFrame_.height() * scale * dpr)));
    displayedFrameSize_ = QSize(qMax(1, int(lastFrame_.width() * scale)),
                                qMax(1, int(lastFrame_.height() * scale)));
    const QRect contentRect = displayLabel_->contentsRect();
    displayedFrameOrigin_ = contentRect.topLeft()
                            + QPoint((contentRect.width() - displayedFrameSize_.width()) / 2,
                                     (contentRect.height() - displayedFrameSize_.height()) / 2);
    QPixmap pixmap = QPixmap::fromImage(lastFrame_).scaled(
        physicalScaledSize,
        Qt::IgnoreAspectRatio,
        integerFriendly ? Qt::FastTransformation : Qt::SmoothTransformation);
    pixmap.setDevicePixelRatio(dpr);
    displayLabel_->setPixmap(pixmap);
}

QImage VirtualMachineWindow::convertInProcessFrame(const vw::MvFrame& frame) const {
    if (frame.pixels.empty() || frame.width == 0 || frame.height == 0) {
        return QImage();
    }
    const int w = static_cast<int>(frame.width);
    const int h = static_cast<int>(frame.height);
    const int bytesPerLine = (w + 7) / 8;
    const bool oneBit = (frame.depth == 1);
    QImage image(w, h, QImage::Format_RGB32);
    if (image.isNull()) {
        return QImage();
    }
    const uint8_t* src = frame.pixels.data();
    for (int y = 0; y < h; ++y) {
        const uint8_t* row = src + static_cast<size_t>(y) * bytesPerLine;
        uint32_t* dst = reinterpret_cast<uint32_t*>(image.scanLine(y));
        for (int x = 0; x < w; ++x) {
            int bit = 0;
            if (oneBit) {
                // Mini vMac / Mac framebuffer convention: MSB first within a byte.
                bit = (row[x >> 3] >> (7 - (x & 7))) & 1;
            }
            dst[x] = bit ? 0xFFFFFFFFu : 0xFF000000u;
        }
    }
    return image;
}

void VirtualMachineWindow::paintInProcessFrame(const vw::MvFrame& frame) {
    if (!frame.valid) {
        return;
    }
    QImage image = convertInProcessFrame(frame);
    if (image.isNull()) {
        return;
    }
    // Coalesce rapid frames: keep the latest image, post at most one paint job.
    {
        std::lock_guard<std::mutex> lock(inProcessFrameMutex_);
        pendingInProcessFrame_ = std::move(image);
    }
    if (!inProcessFramePending_.exchange(true)) {
        // The callback runs on the engine worker thread; this post lands on the
        // GUI thread. Guard with a QPointer so a window torn down between post and
        // dispatch cannot be touched through a dangling 'this'.
        QMetaObject::invokeMethod(this, [self = QPointer<VirtualMachineWindow>(this)]() {
            if (!self) {
                return;
            }
            self->inProcessFramePending_ = false;
            QImage image;
            {
                std::lock_guard<std::mutex> lock(self->inProcessFrameMutex_);
                image = std::move(self->pendingInProcessFrame_);
            }
            if (image.isNull() || !self->displayLabel_) {
                return;
            }
            const QSize target = self->displayLabel_->contentsRect().size();
            QPixmap pix;
            if (target.width() > 0 && target.height() > 0) {
                pix = QPixmap::fromImage(image).scaled(target, Qt::KeepAspectRatio, Qt::SmoothTransformation);
            } else {
                pix = QPixmap::fromImage(image);
            }
            self->displayLabel_->setPixmap(pix);
            if (!self->firstFrameShown_) {
                self->firstFrameShown_ = true;
                emit self->firstFrameReady();
            }
        }, Qt::QueuedConnection);
    }
}

void VirtualMachineWindow::stopVm() {
    userRequestedStop_ = true;
    releaseInputCapture();
    frameCapturePending_ = false;
    if (frameTimer_) {
        frameTimer_->stop();
    }
    if (isSystemSoftwareConfig(config_)) {
        // In-process engine: join the worker thread, no process to kill.
        systemSoftwareEngine_.stop();
        statusLabel_->setText("已停止");
        closeQmp();
        return;
    }
    if (process_.state() == QProcess::NotRunning) {
        closeQmp();
        return;
    }
    sendMonitorCommand("quit\n");
    if (!process_.waitForFinished(2500)) {
        process_.terminate();
    }
    if (!process_.waitForFinished(1500)) {
        process_.kill();
        process_.waitForFinished(1500);
    }
    closeQmp();
}

} // namespace vw
