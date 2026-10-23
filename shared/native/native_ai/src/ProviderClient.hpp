#pragma once

#include "AppConfig.hpp"

#include <QString>
#include <QVector>

namespace nativeai {

struct ChatMessage {
    QString role;
    QString content;
};

class ProviderClient {
public:
    QString ask(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error = nullptr);
    bool testConnection(const AppConfig& config, QString* message = nullptr);
    QStringList listModels(const AppConfig& config, QString* error = nullptr);

private:
    QString askOllama(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
    QString askLlamaCpp(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
    QString askOpenAICompatible(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error);
};

} // namespace nativeai
