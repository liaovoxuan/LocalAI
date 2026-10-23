#pragma once

#include <QString>
#include <QStringList>

class QWidget;

namespace vw {

QString currentLanguage();
bool hasSelectedLanguage();
QStringList supportedLanguages();
QString languageDisplayName(const QString& code);
void setCurrentLanguage(const QString& code);
QString trText(const QString& key);
bool chooseApplicationLanguage(QWidget* parent, bool firstRun);

} // namespace vw
