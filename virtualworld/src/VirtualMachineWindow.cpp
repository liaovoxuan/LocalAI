#include "VirtualMachineWindow.hpp"

#include <QApplication>
#include <QAction>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QLabel>
#include <QLocalSocket>
#include <QMenu>
#include <QMessageBox>
#include <QPixmap>
#include <QPushButton>
#include <QResizeEvent>
#include <QScreen>
#include <QSizePolicy>
#include <QStandardPaths>
#include <QStyle>
#include <QTimer>
#include <QVBoxLayout>

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

QPushButton* makeToolButton(const QString& text, const QString& tooltip) {
    auto* button = new QPushButton(text);
    button->setToolTip(tooltip);
    button->setFixedSize(42, 36);
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

} // namespace

VirtualMachineWindow::VirtualMachineWindow(VmConfig config, QWidget* parent)
    : QWidget(parent),
      config_(std::move(config)) {
    buildUi();
}

VirtualMachineWindow::~VirtualMachineWindow() {
    stopVm();
}

void VirtualMachineWindow::buildUi() {
    setWindowTitle(vmTitle(config_));
    resize(1280, 820);
    if (QScreen* screen = QApplication::primaryScreen()) {
        const QRect available = screen->availableGeometry();
        resize(qBound(1040, int(available.width() * 0.86), available.width()),
               qBound(720, int(available.height() * 0.86), available.height()));
    }
    setStyleSheet("QWidget{background:#111827;color:#e5e7eb;font-size:14px;}"
                  "QLabel#display{background:#05070a;border:1px solid #253044;border-radius:10px;}"
                  "QPushButton{border-radius:10px;background:#1f2937;color:#f9fafb;border:1px solid #374151;font-weight:700;}"
                  "QPushButton:hover{background:#2b3648;}"
                  "QPushButton#danger:hover{background:#7f1d1d;border-color:#991b1b;}"
                  "QPushButton#active{background:#2563eb;border-color:#60a5fa;}");

    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(14, 14, 14, 14);
    root->setSpacing(10);

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
    displayLabel_->setObjectName("display");
    displayLabel_->setAlignment(Qt::AlignCenter);
    displayLabel_->setMinimumSize(900, 560);
    displayLabel_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    displayLabel_->setScaledContents(false);
    root->addWidget(displayLabel_, 1);

    frameTimer_ = new QTimer(this);
    frameTimer_->setInterval(1600);
    connect(frameTimer_, &QTimer::timeout, this, [this] { captureFrame(); });
    connect(&process_, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this](int exitCode, QProcess::ExitStatus status) {
                frameTimer_->stop();
                statusLabel_->setText(QString("已停止 (%1)").arg(exitCode));
                if (status != QProcess::NormalExit) {
                    displayLabel_->setText("虚拟机进程异常退出。");
                }
            });
}

void VirtualMachineWindow::resizeEvent(QResizeEvent* event) {
    QWidget::resizeEvent(event);
    rescaleLastFrame();
}

void VirtualMachineWindow::keyPressEvent(QKeyEvent* event) {
    const bool commandOrControl = event->modifiers().testFlag(Qt::ControlModifier) ||
                                  event->modifiers().testFlag(Qt::MetaModifier);
    if (mouseLocked_ && commandOrControl && (event->key() == Qt::Key_V || event->key() == Qt::Key_Tab)) {
        toggleMouseLock();
        event->accept();
        return;
    }
    QWidget::keyPressEvent(event);
}

QString VirtualMachineWindow::monitorPath() const {
    return QDir(QStandardPaths::writableLocation(QStandardPaths::TempLocation))
        .filePath(QString("virtualworld-%1.mon").arg(reinterpret_cast<quintptr>(this)));
}

QString VirtualMachineWindow::framePath() const {
    return QDir(QStandardPaths::writableLocation(QStandardPaths::TempLocation))
        .filePath(QString("virtualworld-%1.ppm").arg(reinterpret_cast<quintptr>(this)));
}

bool VirtualMachineWindow::start(QString* error) {
    QFile::remove(monitorPath());
    QFile::remove(framePath());

    VmConfig launch = config_;
    launch.graphics.display = "none";
    launch.spice.enabled = false;
    launch.extraArgs.removeAll("-display");
    launch.extraArgs.removeAll("cocoa");
    launch.extraArgs.removeAll("gtk");
    launch.extraArgs.removeAll("sdl");
    const QStringList extraArgs = {"-monitor", "unix:" + monitorPath() + ",server,nowait"};
    if (!engine_.prepareProcess(process_, launch, extraArgs, error)) {
        return false;
    }

    process_.start();
    if (!process_.waitForStarted(5000)) {
        if (error) {
            *error = process_.errorString();
        }
        return false;
    }
    statusLabel_->setText(QString("运行中 PID %1").arg(process_.processId()));
    QTimer::singleShot(1800, this, [this] { captureFrame(); });
    frameTimer_->start();
    return true;
}

qint64 VirtualMachineWindow::processId() const {
    return process_.processId();
}

bool VirtualMachineWindow::sendMonitorCommand(const QString& command, QByteArray* reply) {
    QLocalSocket socket;
    socket.connectToServer(monitorPath());
    if (!socket.waitForConnected(800)) {
        return false;
    }
    socket.write(command.toUtf8());
    socket.flush();
    socket.waitForBytesWritten(500);
    socket.waitForReadyRead(800);
    if (reply) {
        *reply = socket.readAll();
        while (socket.waitForReadyRead(80)) {
            reply->append(socket.readAll());
        }
    }
    socket.disconnectFromServer();
    return true;
}

void VirtualMachineWindow::captureFrame() {
    if (process_.state() == QProcess::NotRunning) {
        return;
    }
    const QString path = framePath();
    QFile::remove(path);
    QByteArray reply;
    if (!sendMonitorCommand("screendump " + path + "\ninfo status\n", &reply)) {
        statusLabel_->setText("等待画面...");
        return;
    }
    if (reply.contains("VM status: running")) {
        statusLabel_->setText(QString("运行中 PID %1").arg(process_.processId()));
    }
    updateImage(path);
}

void VirtualMachineWindow::togglePower() {
    stopVm();
    close();
}

void VirtualMachineWindow::rebootVm() {
    if (process_.state() == QProcess::NotRunning) {
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
    QAction* replace = menu.addAction("更换安装镜像...");
    QAction* eject = menu.addAction("弹出安装镜像");
    QAction* selected = menu.exec(imageButton_->mapToGlobal(imageButton_->rect().bottomLeft()));
    if (selected == replace) {
        replaceImage();
    } else if (selected == eject) {
        ejectImage();
    }
}

QString VirtualMachineWindow::findFirstCdromDevice(const QByteArray& infoBlock) const {
    const QString text = QString::fromUtf8(infoBlock);
    const QStringList lines = text.split('\n', Qt::SkipEmptyParts);
    QString fallback;
    for (QString line : lines) {
        line = line.trimmed();
        const int colon = line.indexOf(':');
        if (colon <= 0) {
            continue;
        }
        const QString device = line.left(colon).trimmed();
        const QString lower = line.toLower();
        if (lower.contains("cdrom") || lower.contains("cd-rom") || lower.contains("removable=1")) {
            return device;
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
    if (!sendMonitorCommand("eject -f " + device + "\n", &reply)) {
        QMessageBox::warning(this, "VirtualWorld", "弹出安装镜像失败。");
        return;
    }
    statusLabel_->setText("安装镜像已弹出");
}

void VirtualMachineWindow::replaceImage() {
    const QString path = QFileDialog::getOpenFileName(
        this,
        "选择安装镜像",
        QDir::homePath(),
        "Images (*.iso *.img *.cdr *.dmg *.qcow2 *.raw);;All Files (*)");
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
    if (!sendMonitorCommand("change " + device + " " + hmpQuote(path) + "\n", &reply)) {
        QMessageBox::warning(this, "VirtualWorld", "更换安装镜像失败。");
        return;
    }
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
        releaseMouse();
        releaseKeyboard();
        mouseLocked_ = false;
        mouseLockButton_->setObjectName("");
        mouseLockButton_->style()->unpolish(mouseLockButton_);
        mouseLockButton_->style()->polish(mouseLockButton_);
        statusLabel_->setText(QString("运行中 PID %1").arg(process_.processId()));
        return;
    }

    grabMouse();
    grabKeyboard();
    mouseLocked_ = true;
    mouseLockButton_->setObjectName("active");
    mouseLockButton_->style()->unpolish(mouseLockButton_);
    mouseLockButton_->style()->polish(mouseLockButton_);
    statusLabel_->setText("鼠标已锁定");

    if (!suppressMouseLockHint_) {
        QMessageBox box(this);
        box.setWindowTitle("鼠标锁定");
        box.setText("鼠标已锁定在虚拟机窗口中。\n按 Cmd/Ctrl+Tab+V 可退出锁定。");
        QPushButton* ok = box.addButton("知道了", QMessageBox::AcceptRole);
        QPushButton* never = box.addButton("不再提示", QMessageBox::DestructiveRole);
        box.setDefaultButton(ok);
        box.exec();
        if (box.clickedButton() == never) {
            suppressMouseLockHint_ = true;
        }
    }
}

void VirtualMachineWindow::updateImage(const QString& path) {
    QImage image(path);
    if (image.isNull()) {
        return;
    }
    lastFrame_ = image;
    rescaleLastFrame();
}

void VirtualMachineWindow::rescaleLastFrame() {
    if (lastFrame_.isNull() || !displayLabel_) {
        return;
    }
    const QSize target = displayLabel_->size() - QSize(8, 8);
    if (target.width() <= 0 || target.height() <= 0) {
        return;
    }
    const QPixmap pixmap = QPixmap::fromImage(lastFrame_).scaled(target, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    displayLabel_->setPixmap(pixmap);
}

void VirtualMachineWindow::stopVm() {
    if (mouseLocked_) {
        releaseMouse();
        releaseKeyboard();
        mouseLocked_ = false;
    }
    if (frameTimer_) {
        frameTimer_->stop();
    }
    if (process_.state() == QProcess::NotRunning) {
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
}

} // namespace vw
