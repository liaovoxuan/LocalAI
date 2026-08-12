#pragma once

#include "AppConfig.hpp"

#include <QObject>
#include <QString>
#include <QVector>

namespace nativeai {

struct ChatMessage {
    QString role;
    QString content;
};

class ProviderClient : public QObject {
    Q_OBJECT
public:
    explicit ProviderClient(QObject* parent = nullptr);
    QString ask(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error = nullptr);

private:
    QString askOllama(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
    QString askLlamaCpp(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
    QString askOpenAICompatible(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
};

} // namespace nativeai
