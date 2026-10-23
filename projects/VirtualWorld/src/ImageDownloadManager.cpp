#include "ImageDownloadManager.hpp"
#include "VmConfig.hpp"

#include <QDesktopServices>
#include <QCryptographicHash>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QUrl>
#include <QtGlobal>
#include <utility>

namespace vw {

namespace {

bool isSha256Text(const QString& value) {
    return value.size() == 64
        && QRegularExpression("^[0-9a-f]{64}$")
               .match(value.toLower())
               .hasMatch();
}

}

ImageDownloadManager::ImageDownloadManager(QObject* parent) : QObject(parent) {}

QString defaultAppleVirtualMachineIdentifier(const QString& arch) {
    Q_UNUSED(arch);
    return "VirtualMac2,1";
}

QString microsoftWindows11DownloadPage() {
    return "https://www.microsoft.com/software-download/windows11";
}

bool isOfficialAppleIpswUrl(const QUrl& url) {
    const QString host = url.host().toLower();
    return url.scheme().compare("https", Qt::CaseInsensitive) == 0
        && (host == "apple.com" || host.endsWith(".apple.com"));
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
                    {},
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
                if (downloadUrl.isEmpty() || !isOfficialAppleIpswUrl(QUrl(downloadUrl))) {
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
                    "Apple 官方 CDN",
                    "Apple 官方 HTTPS IPSW 恢复镜像。下载后将记录来源并在恢复流程中复核。",
                    isSha256Text(object.value("sha256").toString().trimmed())
                        ? object.value("sha256").toString().trimmed().toLower()
                        : QString(),
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
    if (image.os.compare("macos", Qt::CaseInsensitive) == 0
        && !isOfficialAppleIpswUrl(QUrl(image.url))) {
        done("已拒绝非 Apple 官方 HTTPS IPSW 来源。");
        return;
    }

    const QString temporaryPath = targetPath + ".download";
    QFile::remove(temporaryPath);
    QFile* output = new QFile(temporaryPath, this);
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
    connect(reply, &QNetworkReply::finished, this, [reply, output, image, targetPath, temporaryPath, done = std::move(done)]() mutable {
        output->write(reply->readAll());
        output->close();
        QString error;
        if (reply->error() != QNetworkReply::NoError) {
            error = reply->errorString();
            output->remove();
        } else if (image.os.compare("macos", Qt::CaseInsensitive) == 0
                   && !isOfficialAppleIpswUrl(reply->url())) {
            error = "下载重定向的最终地址不是 Apple 官方 HTTPS 域名。";
            output->remove();
        } else if (!image.sha256.trimmed().isEmpty()) {
            QFile payload(temporaryPath);
            if (!payload.open(QIODevice::ReadOnly)
                || QCryptographicHash::hash(payload.readAll(), QCryptographicHash::Sha256).toHex()
                       .compare(image.sha256.trimmed().toLatin1(), Qt::CaseInsensitive) != 0) {
                error = "下载的 IPSW SHA-256 与目录提供的值不一致。";
                output->remove();
            }
        }
        if (error.isEmpty()) {
            QFile::remove(targetPath);
            if (!QFile::rename(temporaryPath, targetPath)) {
                error = "无法完成 IPSW 原子保存。";
                QFile::remove(temporaryPath);
            }
        }
        reply->deleteLater();
        output->deleteLater();
        done(error);
    });
}

} // namespace vw
