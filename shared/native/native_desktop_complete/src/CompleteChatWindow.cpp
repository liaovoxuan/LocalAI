#include "CompleteChatWindow.hpp"

#include <QComboBox>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMessageBox>
#include <QPalette>
#include <QPointer>
#include <QPushButton>
#include <QScrollArea>
#include <QShortcut>
#include <QStatusBar>
#include <QTextBrowser>
#include <QTextEdit>
#include <QThread>
#include <QToolBar>
#include <QToolButton>
#include <QVBoxLayout>

namespace nativeai {

ChatWindow::ChatWindow(AppMode mode, QWidget* parent)
    : QMainWindow(parent), mode_(mode), config_(loadConfig(mode)) { buildUi(); }

void ChatWindow::buildUi() {
    setWindowTitle(appName(mode_) + " " + versionLabel(mode_));
    setMinimumSize(720, 560); resize(1100, 760);
    QToolBar* toolbar = addToolBar("main"); toolbar->setMovable(false);
    QLabel* brand = new QLabel("  LocalAI 1.0  "); brand->setStyleSheet("font-size:18px;font-weight:700;"); toolbar->addWidget(brand);
    toolbar->addSeparator();
    toolbar->addAction(trText(config_.language, "new_chat"), this, [this] {
        ++requestSerial_; sending_ = false; messages_.clear(); transcript_->clear(); currentChatPath_.clear();
        importedFiles_.clear(); updateAttachmentLabel(); statusBar()->clearMessage();
    });
    QPushButton* history = new QPushButton(trText(config_.language, "history")); toolbar->addWidget(history);
    connect(history, &QPushButton::clicked, this, [this] { historyList_->setVisible(!historyList_->isVisible()); if (historyList_->isVisible()) refreshHistory(); });
    agentButton_ = new QPushButton(trText(config_.language, "agent_off")); agentButton_->setCheckable(true); agentButton_->setToolTip(trText(config_.language, "agent_tooltip")); toolbar->addWidget(agentButton_);
    connect(agentButton_, &QPushButton::toggled, this, [this](bool on) { agentMode_ = on; agentButton_->setText(trText(config_.language, on ? "agent_on" : "agent_off")); statusBar()->showMessage(trText(config_.language, on ? "agent_enabled" : "agent_disabled"), 2200); });
    toolbar->addAction(trText(config_.language, "import_file"), this, [this] { importFiles(); });
    toolbar->addAction(trText(config_.language, "export"), this, [this] { exportCurrentChat(); });
    toolbar->addAction(trText(config_.language, "settings"), this, [this] { openSettings(); });

    QWidget* central = new QWidget(); QHBoxLayout* outer = new QHBoxLayout(central); outer->setContentsMargins(12, 10, 12, 12); outer->setSpacing(12);
    historyList_ = new QListWidget(); historyList_->setFixedWidth(230); historyList_->setVisible(false); outer->addWidget(historyList_);
    connect(historyList_, &QListWidget::itemActivated, this, [this](QListWidgetItem* item) { loadChat(item->data(Qt::UserRole).toString()); });
    QVBoxLayout* conversation = new QVBoxLayout(); conversation->setSpacing(10);
    transcript_ = new QTextBrowser(); transcript_->setOpenExternalLinks(true); conversation->addWidget(transcript_, 1);
    attachmentLabel_ = new QLabel(); attachmentLabel_->setVisible(false); attachmentLabel_->setTextInteractionFlags(Qt::TextSelectableByMouse); conversation->addWidget(attachmentLabel_);
    QHBoxLayout* composer = new QHBoxLayout();
    QToolButton* attach = new QToolButton(); attach->setText("+"); attach->setToolTip(trText(config_.language, "import_file")); attach->setFixedSize(46, 46);
    input_ = new QTextEdit(); input_->setAcceptRichText(false); input_->setFixedHeight(88);
    sendButton_ = new QPushButton(trText(config_.language, "send")); sendButton_->setFixedSize(86, 46); sendButton_->setEnabled(false);
    composer->addWidget(attach); composer->addWidget(input_, 1); composer->addWidget(sendButton_, 0, Qt::AlignBottom); conversation->addLayout(composer); outer->addLayout(conversation, 1); setCentralWidget(central);
    connect(attach, &QToolButton::clicked, this, [this] { importFiles(); });
    connect(sendButton_, &QPushButton::clicked, this, [this] { sendMessage(); });
    connect(input_, &QTextEdit::textChanged, this, [this] { sendButton_->setEnabled(!sending_ && !input_->toPlainText().trimmed().isEmpty()); });
    QShortcut* shortcut = new QShortcut(QKeySequence(Qt::CTRL | Qt::Key_Return), input_); connect(shortcut, &QShortcut::activated, this, [this] { sendMessage(); });
    refreshHistory(); applyTheme();
}

void ChatWindow::applyTheme() {
    const bool dark = config_.theme == "dark" || (config_.theme == "auto" && palette().color(QPalette::Window).lightness() < 128);
    setStyleSheet(dark
        ? "QMainWindow,QWidget{background:#11161a;color:#e8edf2;font-size:14px;}QToolBar{background:#151b20;border-bottom:1px solid #303940;spacing:8px;padding:8px;}QTextBrowser,QTextEdit,QLineEdit,QComboBox,QListWidget{background:#0d1114;color:#edf2f5;border:1px solid #3a454d;border-radius:5px;padding:8px;}QPushButton,QToolButton{background:#273038;color:#eef3f5;border:0;border-radius:5px;padding:9px 15px;}QPushButton:checked,QPushButton:hover,QToolButton:hover{background:#2b9e70;}QPushButton:disabled{color:#758089;}"
        : "QMainWindow,QWidget{background:#f5f7f8;color:#172027;font-size:14px;}QToolBar{background:#ffffff;border-bottom:1px solid #d7dde1;spacing:8px;padding:8px;}QTextBrowser,QTextEdit,QLineEdit,QComboBox,QListWidget{background:#ffffff;color:#172027;border:1px solid #c8d0d6;border-radius:5px;padding:8px;}QPushButton,QToolButton{background:#273038;color:white;border:0;border-radius:5px;padding:9px 15px;}QPushButton:checked,QPushButton:hover,QToolButton:hover{background:#23835e;}QPushButton:disabled{background:#c8d0d6;}" );
}

void ChatWindow::appendBubble(const QString& speaker, const QString& text, bool user) {
    const QString align = user ? "right" : "left"; const QString background = user ? "#276be8" : "#273038";
    transcript_->append(QString("<div align='%1'><table cellpadding='10' style='background:%2;color:#ffffff;border-radius:5px;max-width:82%%'><tr><td><b>%3</b><br>%4</td></tr></table></div><br>")
        .arg(align, background, speaker.toHtmlEscaped(), text.toHtmlEscaped().replace("\n", "<br>")));
}

bool ChatWindow::attachmentContext(QString* context, QString* error) const {
    constexpr qint64 perFile = 128 * 1024; constexpr qint64 totalLimit = 512 * 1024; qint64 total = 0; QStringList blocks;
    for (const QString& path : importedFiles_) {
        QFile file(path); if (!file.open(QIODevice::ReadOnly)) { if (error) *error = QFileInfo(path).fileName() + ": " + file.errorString(); return false; }
        QByteArray data = file.read(perFile + 1); if (data.contains('\0')) { if (error) *error = QFileInfo(path).fileName() + " is binary; attach a text export instead."; return false; }
        if (data.size() > perFile) data.truncate(perFile); total += data.size(); if (total > totalLimit) { if (error) *error = "Attachments exceed the 512 KiB request limit."; return false; }
        blocks << "[Attached file: " + QFileInfo(path).fileName() + "]\n" + QString::fromUtf8(data);
    }
    if (context) *context = blocks.join("\n\n"); return true;
}

void ChatWindow::sendMessage() {
    if (sending_) return; const QString question = input_->toPlainText().trimmed(); if (question.isEmpty()) return;
    QString attachments, attachmentError; if (!attachmentContext(&attachments, &attachmentError)) { QMessageBox::warning(this, trText(config_.language, "error"), attachmentError); return; }
    input_->clear(); messages_.append({"user", question}); appendBubble(trText(config_.language, "user"), question, true); saveCurrentChat(); sending_ = true; sendButton_->setEnabled(false); statusBar()->showMessage(trText(config_.language, "thinking"));
    QVector<ChatMessage> request = messages_; if (!attachments.isEmpty()) request.last().content += "\n\n" + attachments; importedFiles_.clear(); updateAttachmentLabel();
    if (agentMode_) request.prepend({"system", "Agent mode is enabled. Use only supplied conversation and attached file contents. Never claim an action that was not performed. Return clear steps and an actionable result."});
    const AppConfig config = config_; const quint64 id = ++requestSerial_; QPointer<ChatWindow> target(this);
    QThread* worker = QThread::create([target, id, request, config] { ProviderClient provider; QString error; const QString answer = provider.ask(request, config, &error); if (target) QMetaObject::invokeMethod(target.data(), [target, id, answer, error] { if (target) target->finishRequest(id, answer, error); }, Qt::QueuedConnection); });
    connect(worker, &QThread::finished, worker, &QObject::deleteLater); worker->start();
}

void ChatWindow::finishRequest(quint64 id, const QString& answer, const QString& error) {
    if (id != requestSerial_) return; sending_ = false; statusBar()->clearMessage(); sendButton_->setEnabled(!input_->toPlainText().trimmed().isEmpty());
    if (!error.isEmpty()) { QMessageBox::warning(this, trText(config_.language, "error"), error); return; }
    messages_.append({"assistant", answer}); appendBubble(appName(mode_), answer, false); saveCurrentChat();
}

void ChatWindow::saveCurrentChat() {
    const QString dir = QDir(config_.appDataDir).filePath("chats"); QDir().mkpath(dir); if (currentChatPath_.isEmpty()) currentChatPath_ = QDir(dir).filePath(QDateTime::currentDateTime().toString("yyyyMMdd-hhmmss-zzz") + ".json");
    QJsonArray items; for (const ChatMessage& message : messages_) items.append(QJsonObject{{"role", message.role}, {"content", message.content}}); QFile file(currentChatPath_); if (file.open(QIODevice::WriteOnly)) file.write(QJsonDocument(QJsonObject{{"messages", items}}).toJson()); refreshHistory();
}

void ChatWindow::refreshHistory() { historyList_->clear(); const QDir dir(QDir(config_.appDataDir).filePath("chats")); for (const QFileInfo& info : dir.entryInfoList({"*.json"}, QDir::Files, QDir::Time)) { QListWidgetItem* item = new QListWidgetItem(info.completeBaseName(), historyList_); item->setData(Qt::UserRole, info.absoluteFilePath()); } }

void ChatWindow::loadChat(const QString& path) {
    QFile file(path); if (!file.open(QIODevice::ReadOnly)) return; messages_.clear(); transcript_->clear();
    for (const QJsonValue& value : QJsonDocument::fromJson(file.readAll()).object().value("messages").toArray()) { const QJsonObject object = value.toObject(); const ChatMessage message{object.value("role").toString(), object.value("content").toString()}; messages_.append(message); appendBubble(message.role == "user" ? trText(config_.language, "user") : appName(mode_), message.content, message.role == "user"); }
    currentChatPath_ = path; input_->setFocus();
}

void ChatWindow::exportCurrentChat() {
    if (messages_.isEmpty()) return; const QString path = QFileDialog::getSaveFileName(this, trText(config_.language, "export"), "LocalAI-chat.json", "JSON (*.json);;Text (*.txt)"); if (path.isEmpty()) return;
    QFile file(path); if (!file.open(QIODevice::WriteOnly)) { QMessageBox::warning(this, trText(config_.language, "error"), file.errorString()); return; }
    if (path.endsWith(".txt", Qt::CaseInsensitive)) { QStringList lines; for (const ChatMessage& message : messages_) lines << message.role + ":\n" + message.content + "\n"; file.write(lines.join('\n').toUtf8()); }
    else { QJsonArray items; for (const ChatMessage& message : messages_) items.append(QJsonObject{{"role", message.role}, {"content", message.content}}); file.write(QJsonDocument(QJsonObject{{"messages", items}}).toJson()); }
    statusBar()->showMessage(trText(config_.language, "export_done"), 2500);
}

void ChatWindow::importFiles() { const QStringList paths = QFileDialog::getOpenFileNames(this, trText(config_.language, "import_file")); if (!paths.isEmpty()) importedFiles_ = paths; updateAttachmentLabel(); }

void ChatWindow::updateAttachmentLabel() { QStringList names; for (const QString& path : importedFiles_) names << QFileInfo(path).fileName(); attachmentLabel_->setVisible(!names.isEmpty()); attachmentLabel_->setText(names.isEmpty() ? QString() : trText(config_.language, "imported_files").arg(names.size()) + "  |  " + names.join(", ")); }

void ChatWindow::openSettings() {
    QDialog dialog(this); dialog.setWindowTitle(trText(config_.language, "settings")); dialog.resize(620, 520); QVBoxLayout* root = new QVBoxLayout(&dialog); QScrollArea* scroll = new QScrollArea(); scroll->setWidgetResizable(true); QWidget* content = new QWidget(); QFormLayout* form = new QFormLayout(content); form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    QComboBox* language = new QComboBox(); language->addItems(supportedLanguages()); language->setCurrentText(config_.language); QComboBox* provider = new QComboBox(); provider->addItems(supportedProviders(mode_)); provider->setCurrentText(config_.provider); QComboBox* model = new QComboBox(); model->setEditable(true); model->addItem(config_.model); QLineEdit* baseUrl = new QLineEdit(config_.apiBaseUrl); QLineEdit* apiKey = new QLineEdit(config_.apiKey); apiKey->setEchoMode(QLineEdit::Password); QLineEdit* gguf = new QLineEdit(config_.llamaCppModel); QLineEdit* binary = new QLineEdit(config_.llamaCppBinary); QComboBox* theme = new QComboBox(); theme->addItems({"auto", "light", "dark"}); theme->setCurrentText(config_.theme);
    QPushButton* pickGguf = new QPushButton(trText(config_.language, "choose")); QPushButton* pickBinary = new QPushButton(trText(config_.language, "choose")); QPushButton* refresh = new QPushButton("Refresh"); QWidget* modelRow = new QWidget(); QHBoxLayout* modelLayout = new QHBoxLayout(modelRow); modelLayout->setContentsMargins(0,0,0,0); modelLayout->addWidget(model); modelLayout->addWidget(refresh); QWidget* ggufRow = new QWidget(); QHBoxLayout* ggufLayout = new QHBoxLayout(ggufRow); ggufLayout->setContentsMargins(0,0,0,0); ggufLayout->addWidget(gguf); ggufLayout->addWidget(pickGguf); QWidget* binRow = new QWidget(); QHBoxLayout* binLayout = new QHBoxLayout(binRow); binLayout->setContentsMargins(0,0,0,0); binLayout->addWidget(binary); binLayout->addWidget(pickBinary);
    form->addRow(trText(config_.language,"language"), language); form->addRow(trText(config_.language,"provider"), provider); form->addRow(trText(config_.language,"model"), modelRow); form->addRow("llama.cpp GGUF", ggufRow); form->addRow("llama.cpp Binary", binRow); form->addRow("Base URL", baseUrl); form->addRow("API Key", apiKey); form->addRow(trText(config_.language,"theme"), theme); scroll->setWidget(content); root->addWidget(scroll, 1); QDialogButtonBox* buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel); QPushButton* test = buttons->addButton(trText(config_.language,"test_connection"), QDialogButtonBox::ActionRole); root->addWidget(buttons);
    connect(pickGguf, &QPushButton::clicked, &dialog, [this, gguf] { const QString path = QFileDialog::getOpenFileName(this, "GGUF", {}, "GGUF (*.gguf)"); if (!path.isEmpty()) gguf->setText(path); }); connect(pickBinary, &QPushButton::clicked, &dialog, [this, binary] { const QString path = QFileDialog::getOpenFileName(this, "llama.cpp executable"); if (!path.isEmpty()) binary->setText(path); });
    auto candidate = [this, provider, model, baseUrl, apiKey, gguf, binary, language, theme] { AppConfig value = config_; value.provider = provider->currentText(); value.model = model->currentText().trimmed(); value.apiBaseUrl = baseUrl->text().trimmed(); value.apiKey = apiKey->text(); value.llamaCppModel = gguf->text().trimmed(); value.llamaCppBinary = binary->text().trimmed(); value.language = language->currentText(); value.theme = theme->currentText(); return value; };
    connect(refresh, &QPushButton::clicked, &dialog, [this, model, candidate] { ProviderClient client; QString error; const QStringList choices = client.listModels(candidate(), &error); if (!error.isEmpty()) QMessageBox::warning(this, trText(config_.language,"error"), error); else { model->clear(); model->addItems(choices); } }); connect(test, &QPushButton::clicked, &dialog, [this, candidate] { ProviderClient client; QString message; const bool okay = client.testConnection(candidate(), &message); QMessageBox::information(this, okay ? "OK" : trText(config_.language,"error"), message); }); connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept); connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if (dialog.exec() != QDialog::Accepted) return; config_ = candidate(); QString error; if (!saveConfig(mode_, config_, &error)) QMessageBox::warning(this, trText(config_.language,"error"), error); applyTheme();
}

} // namespace nativeai
