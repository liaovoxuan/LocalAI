#pragma once

#include "Platform.hpp"

#include <QNetworkAccessManager>
#include <QObject>
#include <QString>
#include <QVector>
#include <functional>

namespace vw {

struct ImageInfo {
    QString id;
    QString name;
    QString os;
    QString architecture;
    QString version;
    QString url;
    QString source;
    QString notes;
    bool directDownload = true;
};

class ImageDownloadManager : public QObject {
public:
    explicit ImageDownloadManager(QObject* parent = nullptr);

    QVector<ImageInfo> builtInImages(const QString& os, const QString& arch = QString()) const;
    void fetchMacOsIpswList(const QString& arch,
                            std::function<void(QVector<ImageInfo>, QString)> done);
    void download(const ImageInfo& image,
                  const QString& targetPath,
                  std::function<void(qint64, qint64)> progress,
                  std::function<void(QString)> done);

private:
    QNetworkAccessManager network_;
};

QString defaultAppleVirtualMachineIdentifier(const QString& arch);
QString microsoftWindows11DownloadPage();

} // namespace vw
