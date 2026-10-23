#include "ChatWindow.hpp"

#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QLineEdit>
#include <QListWidget>
#include <QFileDialog>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QDateTime>
#include <QShortcut>
#include <QMessageBox>
#include <QPushButton>
#include <QToolButton>
#include <QScrollArea>
#include <QPointer>
#include <QStatusBar>
#include <QTextEdit>
#include <QTextBrowser>
#include <QThread>
#include <QToolBar>
#include <QVBoxLayout>
#include <QPalette>

namespace nativeai {

ChatWindow::ChatWindow(AppMode mode, QWidget* parent)
    : QMainWindow(parent), mode_(mode), config_(loadConfig(mode)) {
    buildUi();
}

void ChatWindow::buildUi() {
    setWindowTitle(appName(mode_) + " " + versionLabel(mode_));
    resize(1100, 760);
    auto* toolbar = addToolBar("main");
    toolbar->setMovable(false);
    toolbar->addAction(appName(mode_) + " " + versionLabel(mode_));
    toolbar->addSeparator();
    toolbar->addAction(trText(config_.language, "new_chat"), this, [this] {
        ++requestSerial_;
        sending_ = false;
        messages_.clear();
        transcript_->clear();
        statusBar()->clearMessage();
        sendButton_->setEnabled(!input_->toPlainText().trimmed().isEmpty());
        currentChatPath_.clear();
    });
    historyButton_ = new QPushButton(trText(config_.language, "history"));
    toolbar->addWidget(historyButton_);
    connect(historyButton_, &QPushButton::clicked, this, [this] {
        historyVisible_ = !historyVisible_;
        historyList_->setVisible(historyVisible_);
        if (historyVisible_) refreshHistory();
    });
    auto* agentButton = new QPushButton("Agent: Off");
    agentButton->setCheckable(true);
    agentButton->setToolTip("Toggle Agent mode");
    toolbar->addWidget(agentButton);
    connect(agentButton, &QPushButton::toggled, this, [this, agentButton](bool enabled) {
        agentMode_ = enabled;
        agentButton->setText(enabled ? "Agent: On" : "Agent: Off");
        statusBar()->showMessage(enabled ? "Agent mode enabled" : "Agent mode disabled", 2500);
    });
    toolbar->addAction(trText(config_.language, "import_file"), this, [this] { importFiles(); });
    toolbar->addAction(trText(config_.language, "export"), this, [this] { exportCurrentChat(); });
    toolbar->addAction(trText(config_.language, "settings"), this, [this] { openSettings(); });

    auto* central = new QWidget();
    auto* outer = new QHBoxLayout(central);
    auto* layout = new QVBoxLayout();
    historyList_ = new QListWidget();
    historyList_->setFixedWidth(230);
    historyList_->setVisible(false);
    outer->addWidget(historyList_);
    outer->addLayout(layout, 1);
    connect(historyList_, &QListWidget::itemActivated, this, [this](QListWidgetItem* item) { loadChat(item->data(Qt::UserRole).toString()); });
    transcript_ = new QTextBrowser();
    transcript_->setReadOnly(true);
    input_ = new QTextEdit();
    input_->setFixedHeight(92);
    sendButton_ = new QPushButton(trText(config_.language, "send"));
    sendButton_->setEnabled(false);
    auto* bottom = new QHBoxLayout();
    auto* attachButton = new QToolButton();
    attachButton->setText("+");
    attachButton->setToolTip(trText(config_.language, "import_file"));
    attachButton->setFixedSize(52, 52);
    attachButton->setStyleSheet("QToolButton{font-size:26px;font-weight:bold;background:#e5e7eb;color:#111827;border:0;border-radius:8px;} QToolButton:hover{background:#dbeafe;}");
    bottom->addWidget(attachButton);
    bottom->addWidget(input_, 1);
    bottom->addWidget(sendButton_);
    layout->addWidget(transcript_, 1);
    layout->addLayout(bottom);
    setCentralWidget(central);
    connect(sendButton_, &QPushButton::clicked, this, [this] { sendMessage(); });
    connect(attachButton, &QToolButton::clicked, this, [this] { importFiles(); });
    connect(input_, &QTextEdit::textChanged, this, [this] {
        sendButton_->setEnabled(!input_->toPlainText().trimmed().isEmpty());
    });
    auto* sendShortcut = new QShortcut(QKeySequence(Qt::Key_Return), input_);
    connect(sendShortcut, &QShortcut::activated, this, [this] { sendMessage(); });
    refreshHistory();
    applyTheme();
}

void ChatWindow::applyTheme() {
    const bool dark = config_.theme == "dark"
        || (config_.theme == "auto" && palette().color(QPalette::Window).lightness() < 128);
    setStyleSheet(dark
        ? "QWidget{background:#0f172a;color:#e5e7eb;font-size:14px;} QTextEdit{background:#111827;color:#f8fafc;border:1px solid #334155;border-radius:8px;padding:10px;} QPushButton{background:#2563eb;color:white;border:0;border-radius:8px;padding:10px 18px;} QPushButton:disabled{background:#475569;color:#cbd5e1;}"
        : "QWidget{background:#f8fafc;color:#111827;font-size:14px;} QTextEdit{background:white;color:#111827;border:1px solid #cbd5e1;border-radius:8px;padding:10px;} QPushButton{background:#2563eb;color:white;border:0;border-radius:8px;padding:10px 18px;} QPushButton:disabled{background:#cbd5e1;color:#64748b;}");
}

void ChatWindow::appendBubble(const QString& speaker, const QString& text, bool user) {
    const QString align = user ? "right" : "left";
    const QString bg = user ? "#2563eb" : "#e5e7eb";
    const QString fg = user ? "#ffffff" : "#111827";
    transcript_->append(QString("<div align='%1'><span style='background:%2;color:%3;border-radius:10px;padding:8px;'><b>%4</b><br>%5</span></div><br>")
        .arg(align, bg, fg, speaker, text.toHtmlEscaped().replace("\n", "<br>")));
}

void ChatWindow::sendMessage() {
    if (sending_) return;
    const QString question = input_->toPlainText().trimmed();
    if (question.isEmpty()) return;
    input_->clear();
    messages_.append({"user", question});
    appendBubble("你", question, true);
    saveCurrentChat();
    sending_ = true;
    sendButton_->setEnabled(false);
    statusBar()->showMessage(trText(config_.language, "thinking"));

    const quint64 requestId = ++requestSerial_;
    QVector<ChatMessage> messages = messages_;
    if (agentMode_) {
        messages.prepend({"system", "You are in Agent mode. Break complex requests into clear steps, use available context, and provide an actionable final result."});
    }
    const AppConfig config = config_;
    QPointer<ChatWindow> target(this);
    QThread* worker = QThread::create([target, requestId, messages, config] {
        ProviderClient client;
        QString error;
        const QString answer = client.ask(messages, config, &error);
        if (!target) return;
        QMetaObject::invokeMethod(target.data(), [target, requestId, answer, error] {
            if (target) {
                target->finishMessageRequest(requestId, answer, error);
            }
        }, Qt::QueuedConnection);
    });
    connect(worker, &QThread::finished, worker, &QObject::deleteLater);
    worker->start();
}

void ChatWindow::finishMessageRequest(quint64 requestId, const QString& answer, const QString& error) {
    if (requestId != requestSerial_) {
        return;
    }
    sending_ = false;
    statusBar()->clearMessage();
    sendButton_->setEnabled(!input_->toPlainText().trimmed().isEmpty());
    if (!error.isEmpty()) {
        QMessageBox::warning(this, trText(config_.language, "error"), error);
        return;
    }
    messages_.append({"assistant", answer});
    appendBubble(appName(mode_), answer, false);
    saveCurrentChat();
}

void ChatWindow::saveCurrentChat() {
    const QString chatDir = QDir(config_.appDataDir).filePath("chats");
    QDir().mkpath(chatDir);
    if (currentChatPath_.isEmpty()) currentChatPath_ = QDir(chatDir).filePath(QDateTime::currentDateTime().toString("yyyyMMdd-hhmmss-zzz") + ".json");
    QJsonArray items;
    for (const auto& message : messages_) items.append(QJsonObject{{"role", message.role}, {"content", message.content}});
    QFile file(currentChatPath_);
    if (file.open(QIODevice::WriteOnly)) file.write(QJsonDocument(QJsonObject{{"messages", items}}).toJson());
    refreshHistory();
}

void ChatWindow::refreshHistory() {
    historyList_->clear();
    QDir dir(QDir(config_.appDataDir).filePath("chats"));
    const auto files = dir.entryInfoList({"*.json"}, QDir::Files, QDir::Time);
    for (const auto& info : files) { auto* item = new QListWidgetItem(info.completeBaseName(), historyList_); item->setData(Qt::UserRole, info.absoluteFilePath()); }
}

void ChatWindow::loadChat(const QString& path) {
    QFile file(path); if (!file.open(QIODevice::ReadOnly)) return;
    const auto root = QJsonDocument::fromJson(file.readAll()).object(); messages_.clear(); transcript_->clear();
    for (const auto& value : root.value("messages").toArray()) { const auto object = value.toObject(); const QString role = object.value("role").toString(); const QString content = object.value("content").toString(); messages_.append({role, content}); appendBubble(role == "user" ? trText(config_.language, "user") : appName(mode_), content, role == "user"); }
    currentChatPath_ = path;
    input_->setFocus();
}

void ChatWindow::exportCurrentChat() { saveCurrentChat(); QMessageBox::information(this, appName(mode_), trText(config_.language, "export_done")); }

void ChatWindow::importFiles() { importedFiles_ = QFileDialog::getOpenFileNames(this, trText(config_.language, "import_file")); statusBar()->showMessage(importedFiles_.isEmpty() ? QString() : trText(config_.language, "imported_files").arg(importedFiles_.size())); }

void ChatWindow::openSettings() {
    QDialog dialog(this);
    dialog.setWindowTitle(trText(config_.language, "settings"));
    dialog.resize(560, 520);
    auto* root = new QVBoxLayout(&dialog);
    root->setContentsMargins(16, 16, 16, 14);
    root->setSpacing(12);
    auto* scroll = new QScrollArea();
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    auto* content = new QWidget();
    auto* form = new QFormLayout(content);
    form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    form->setVerticalSpacing(10);
    auto* language = new QComboBox();
    language->addItems(supportedLanguages());
    language->setCurrentText(config_.language);
    auto* provider = new QComboBox();
    provider->addItems(supportedProviders(mode_));
    provider->setCurrentText(config_.provider);
    auto* model = new QLineEdit(config_.model);
    auto* llamaModel = new QLineEdit(config_.llamaCppModel);
    auto* llamaBinary = new QLineEdit(config_.llamaCppBinary);
    auto* baseUrl = new QLineEdit(config_.apiBaseUrl);
    auto* apiKey = new QLineEdit(config_.apiKey);
    apiKey->setEchoMode(QLineEdit::Password);
    auto* theme = new QComboBox();
    theme->addItems({"auto", "light", "dark"});
    theme->setCurrentText(config_.theme);
    form->addRow("Language", language);
    form->addRow("Provider", provider);
    form->addRow("Model", model);
    form->addRow("llama.cpp GGUF", llamaModel);
    form->addRow("llama.cpp Binary", llamaBinary);
    form->addRow("Base URL", baseUrl);
    form->addRow("API Key", apiKey);
    form->addRow("Theme", theme);
    scroll->setWidget(content);
    root->addWidget(scroll, 1);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel);
    auto* testButton = buttons->addButton(trText(config_.language, "test_connection"), QDialogButtonBox::ActionRole);
    root->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(testButton, &QPushButton::clicked, &dialog, [this, provider, model, llamaModel, llamaBinary, baseUrl, apiKey] {
        AppConfig candidate = config_;
        candidate.provider = provider->currentText();
        candidate.model = model->text().trimmed();
        candidate.llamaCppModel = llamaModel->text().trimmed();
        candidate.llamaCppBinary = llamaBinary->text().trimmed();
        candidate.apiBaseUrl = baseUrl->text().trimmed();
        candidate.apiKey = apiKey->text().trimmed();
        ProviderClient client; QString message;
        const bool ok = client.testConnection(candidate, &message);
        QMessageBox::information(this, appName(mode_), message.isEmpty() ? (ok ? "OK" : trText(config_.language, "error")) : message);
    });
    if (dialog.exec() != QDialog::Accepted) return;
    config_.language = language->currentText();
    config_.provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : provider->currentText();
    config_.model = model->text().trimmed();
    config_.llamaCppModel = llamaModel->text().trimmed();
    config_.llamaCppBinary = llamaBinary->text().trimmed();
    config_.apiBaseUrl = baseUrl->text().trimmed();
    config_.apiKey = apiKey->text();
    config_.theme = theme->currentText();
    QString error;
    if (!saveConfig(mode_, config_, &error)) {
        QMessageBox::warning(this, trText(config_.language, "error"), error);
    }
    applyTheme();
}

} // namespace nativeai
