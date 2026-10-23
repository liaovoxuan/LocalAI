#pragma once

#include "AppConfig.hpp"
#include "ProviderClient.hpp"

#include <QMainWindow>

class QComboBox;
class QListWidget;
class QLineEdit;
class QPushButton;
class QTextEdit;
class QTextBrowser;

namespace nativeai {

class ChatWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit ChatWindow(AppMode mode, QWidget* parent = nullptr);

private:
    void buildUi();
    void applyTheme();
    void sendMessage();
    void finishMessageRequest(quint64 requestId, const QString& answer, const QString& error);
    void openSettings();
    void appendBubble(const QString& speaker, const QString& text, bool user);
    void refreshHistory();
    void saveCurrentChat();
    void loadChat(const QString& path);
    void exportCurrentChat();
    void importFiles();

    AppMode mode_;
    AppConfig config_;
    QVector<ChatMessage> messages_;
    QTextBrowser* transcript_ = nullptr;
    QTextEdit* input_ = nullptr;
    QPushButton* sendButton_ = nullptr;
    QPushButton* historyButton_ = nullptr;
    QListWidget* historyList_ = nullptr;
    QString currentChatPath_;
    QStringList importedFiles_;
    bool historyVisible_ = false;
    bool agentMode_ = false;
    bool sending_ = false;
    quint64 requestSerial_ = 0;
};

} // namespace nativeai
