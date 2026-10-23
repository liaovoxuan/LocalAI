#pragma once

#include "AppConfig.hpp"
#include "ProviderClient.hpp"

#include <QMainWindow>

class QLabel;
class QListWidget;
class QPushButton;
class QTextBrowser;
class QTextEdit;

namespace nativeai {

class ChatWindow final : public QMainWindow {
public:
    explicit ChatWindow(AppMode mode, QWidget* parent = nullptr);

private:
    void buildUi();
    void applyTheme();
    void sendMessage();
    void finishRequest(quint64 requestId, const QString& answer, const QString& error);
    void openSettings();
    void appendBubble(const QString& speaker, const QString& text, bool user);
    void refreshHistory();
    void saveCurrentChat();
    void loadChat(const QString& path);
    void exportCurrentChat();
    void importFiles();
    bool attachmentContext(QString* context, QString* error) const;
    void updateAttachmentLabel();

    AppMode mode_;
    AppConfig config_;
    QVector<ChatMessage> messages_;
    QTextBrowser* transcript_ = nullptr;
    QTextEdit* input_ = nullptr;
    QPushButton* sendButton_ = nullptr;
    QPushButton* agentButton_ = nullptr;
    QListWidget* historyList_ = nullptr;
    QLabel* attachmentLabel_ = nullptr;
    QString currentChatPath_;
    QStringList importedFiles_;
    bool agentMode_ = false;
    bool sending_ = false;
    quint64 requestSerial_ = 0;
};

} // namespace nativeai
