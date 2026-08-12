#include "ImageDownloadManager.hpp"
#include "VmConfig.hpp"

#include <QDesktopServices>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QUrl>
#include <QtGlobal>
#include <utility>

namespace vw {

ImageDownloadManager::ImageDownloadManager(QObject* parent) : QObject(parent) {}

QString defaultAppleVirtualMachineIdentifier(const QString& arch) {
    Q_UNUSED(arch);
    return "VirtualMac2,1";
}

QString microsoftWindows11DownloadPage() {
    return "https://www.microsoft.com/software-download/windows11";
}

QVector<ImageInfo> ImageDownloadManager::builtInImages(const QString& os, const QString& arch) const {
    const QString osKey = os.trimmed().toLower();
    QVector<ImageInfo> images;
    if (osKey.contains("windows")) {
        images.append(ImageInfo{
            "windows-official",
            "Windows 官方下载页",
            "windows",
            arch.isEmpty() ? "x86_64/aarch64" : normalizeArchitecture(arch),
            "latest",
            microsoftWindows11DownloadPage(),
            "Microsoft",
            "微软 ISO 下载链接通常由官网页面按地区和会话生成。请在打开的官方页面选择 Windows ISO 后导入。",
            false,
        });
    }
    return images;
}

void ImageDownloadManager::fetchMacOsIpswList(const QString& arch,
                                              std::function<void(QVector<ImageInfo>, QString)> done) {
    const QString identifier = defaultAppleVirtualMachineIdentifier(arch);
    const QUrl url(QString("https://api.ipsw.me/v4/device/%1?type=ipsw").arg(identifier));
    QNetworkRequest request{url};
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    QNetworkReply* reply = network_.get(request);
    connect(reply, &QNetworkReply::finished, this, [reply, arch, done = std::move(done)]() mutable {
        QVector<ImageInfo> images;
        QString error;
        if (reply->error() != QNetworkReply::NoError) {
            error = reply->errorString();
        } else {
            const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
            QJsonArray array;
            if (document.isArray()) {
                array = document.array();
            } else if (document.isObject()) {
                array = document.object().value("firmwares").toArray();
            }
            for (const QJsonValue& value : array) {
                const QJsonObject object = value.toObject();
                const QString downloadUrl = object.value("url").toString();
                if (downloadUrl.isEmpty()) {
                    continue;
                }
                if (object.contains("signed") && !object.value("signed").toBool()) {
                    continue;
                }
                const QString version = object.value("version").toString("macOS");
                const QString build = object.value("buildid").toString();
                images.append(ImageInfo{
                    object.value("identifier").toString("macos-ipsw-" + version),
                    QString("macOS %1 %2 IPSW").arg(version, build).trimmed(),
                    "macos",
                    normalizeArchitecture(arch),
                    version,
                    downloadUrl,
                    downloadUrl.contains("apple.com", Qt::CaseInsensitive) ? "Apple CDN" : "IPSW catalog",
                    "Apple Silicon/macOS Virtualization 使用的 IPSW 恢复镜像。",
                    true,
                });
            }
        }
        reply->deleteLater();
        done(images, error);
    });
}

void ImageDownloadManager::download(const ImageInfo& image,
                                    const QString& targetPath,
                                    std::function<void(qint64, qint64)> progress,
                                    std::function<void(QString)> done) {
    if (!image.directDownload) {
        QDesktopServices::openUrl(QUrl(image.url));
        done("该镜像需要在官方页面确认后下载，请下载完成后手动导入镜像。");
        return;
    }

    QFile* output = new QFile(targetPath, this);
    if (!output->open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        const QString error = output->errorString();
        output->deleteLater();
        done(error);
        return;
    }

    QNetworkRequest request{QUrl(image.url)};
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::NoLessSafeRedirectPolicy);
    QNetworkReply* reply = network_.get(request);
    connect(reply, &QNetworkReply::downloadProgress, this, [progress = std::move(progress)](qint64 received, qint64 total) {
        progress(received, total);
    });
    connect(reply, &QNetworkReply::readyRead, this, [reply, output]() {
        output->write(reply->readAll());
    });
    connect(reply, &QNetworkReply::finished, this, [reply, output, done = std::move(done)]() mutable {
        output->write(reply->readAll());
        output->close();
        QString error;
        if (reply->error() != QNetworkReply::NoError) {
            error = reply->errorString();
            output->remove();
        }
        reply->deleteLater();
        output->deleteLater();
        done(error);
    });
}

} // namespace vw
