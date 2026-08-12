#pragma once

#include "AppConfig.hpp"
#include "ProviderClient.hpp"

#include <QMainWindow>

class QComboBox;
class QLineEdit;
class QPushButton;
class QTextEdit;

namespace nativeai {

class ChatWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit ChatWindow(AppMode mode, QWidget* parent = nullptr);

private:
    void buildUi();
    void applyTheme();
    void sendMessage();
    void openSettings();
    void appendBubble(const QString& speaker, const QString& text, bool user);

    AppMode mode_;
    AppConfig config_;
    ProviderClient client_;
    QVector<ChatMessage> messages_;
    QTextEdit* transcript_ = nullptr;
    QTextEdit* input_ = nullptr;
    QPushButton* sendButton_ = nullptr;
};

} // namespace nativeai
