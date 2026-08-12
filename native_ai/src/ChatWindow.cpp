#include "ChatWindow.hpp"

#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QLineEdit>
#include <QMessageBox>
#include <QPushButton>
#include <QScrollArea>
#include <QTextEdit>
#include <QToolBar>
#include <QVBoxLayout>

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
        messages_.clear();
        transcript_->clear();
    });
    toolbar->addAction(trText(config_.language, "settings"), this, [this] { openSettings(); });

    auto* central = new QWidget();
    auto* layout = new QVBoxLayout(central);
    transcript_ = new QTextEdit();
    transcript_->setReadOnly(true);
    input_ = new QTextEdit();
    input_->setFixedHeight(92);
    sendButton_ = new QPushButton(trText(config_.language, "send"));
    sendButton_->setEnabled(false);
    auto* bottom = new QHBoxLayout();
    bottom->addWidget(input_, 1);
    bottom->addWidget(sendButton_);
    layout->addWidget(transcript_, 1);
    layout->addLayout(bottom);
    setCentralWidget(central);
    connect(sendButton_, &QPushButton::clicked, this, [this] { sendMessage(); });
    connect(input_, &QTextEdit::textChanged, this, [this] {
        sendButton_->setEnabled(!input_->toPlainText().trimmed().isEmpty());
    });
    applyTheme();
}

void ChatWindow::applyTheme() {
    const bool dark = config_.theme == "dark";
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
    const QString question = input_->toPlainText().trimmed();
    if (question.isEmpty()) return;
    input_->clear();
    messages_.append({"user", question});
    appendBubble("你", question, true);
    appendBubble(appName(mode_), trText(config_.language, "thinking"), false);
    QString error;
    const QString answer = client_.ask(messages_, config_, &error);
    if (!error.isEmpty()) {
        QMessageBox::warning(this, trText(config_.language, "error"), error);
        return;
    }
    messages_.append({"assistant", answer});
    appendBubble(appName(mode_), answer, false);
}

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
    provider->addItems(mode_ == AppMode::CloudAI ? QStringList{"openai_compatible"} : QStringList{"ollama", "llama_cpp", "lm_studio", "openai_compatible"});
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
    root->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if (dialog.exec() != QDialog::Accepted) return;
    config_.language = language->currentText();
    config_.provider = provider->currentText();
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
