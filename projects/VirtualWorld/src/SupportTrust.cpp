#include "SupportTrust.hpp"

#include <QCryptographicHash>
#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QRegularExpression>

#include <openssl/evp.h>

namespace vw {
namespace {

// Public keys only. Corresponding private keys are release-infrastructure
// secrets and must never be stored in this repository or application bundle.
constexpr auto kSupportPublicKeyId = "virtualworld-support-ed25519-v1";
constexpr auto kSupportPublicKey = "jfMFYzfvIL7vFSZQ3run+aTyCaxUk1Reyf/YDMIXB6U=";
constexpr auto kLinuxSupportPublicKeyId = "virtualworld-linux-support-ed25519-v1";
constexpr auto kLinuxSupportPublicKey = "vjnQv+Scb817lzkFhxzBziMZ3BhREhpmjETGlg3pAgQ=";
constexpr auto kMacosSupportPublicKeyId = "virtualworld-macos-support-ed25519-v1";
constexpr auto kMacosSupportPublicKey = "K5aJairVT/vE+fQVOqqqYWfi6ymF97z1daOjOatAxcY=";
constexpr auto kAuthorizationPublicKeyId = "virtualworld-authorization-ed25519-v1";
constexpr auto kAuthorizationPublicKey = "SfHk9DrqmBxyt2Fte9XkvzZgpu3IgdC72oqt/6NIdTw=";
constexpr auto kManifestFormat = "virtualworld-support-disk-v1";

QString requiredString(const QJsonObject& object, const char* key) {
    return object.value(QLatin1String(key)).toString().trimmed();
}

QByteArray canonicalManifest(const QJsonObject& manifest) {
    const QStringList keys = {
        "format", "product", "version", "source", "payload",
        "sha256", "issued_at", "expires_at"
    };
    QByteArray result;
    for (const QString& key : keys) {
        result += key.toUtf8();
        result += '=';
        result += manifest.value(key).toString().trimmed().toUtf8();
        result += '\n';
    }
    return result;
}

QByteArray canonicalAuthorization(const QJsonObject& token) {
    const QStringList keys = {
        "format", "operation", "resource", "issued_at", "expires_at", "key_id"
    };
    QByteArray result;
    for (const QString& key : keys) {
        result += key.toUtf8();
        result += '=';
        result += token.value(key).toString().trimmed().toUtf8();
        result += '\n';
    }
    return result;
}

bool isSha256(const QString& value) {
    return value.size() == 64
        && QRegularExpression("^[0-9a-f]{64}$").match(value.toLower()).hasMatch();
}

bool readJson(const QString& path, QJsonObject* object, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) *error = "无法读取信任清单：" + file.errorString();
        return false;
    }
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        if (error) *error = "信任清单不是有效 JSON：" + parseError.errorString();
        return false;
    }
    *object = document.object();
    return true;
}

bool verifyTimeWindow(const QJsonObject& object, QString* error) {
    bool issuedOk = false;
    bool expiresOk = false;
    const qint64 issued = object.value("issued_at").toString().toLongLong(&issuedOk);
    const qint64 expires = object.value("expires_at").toString().toLongLong(&expiresOk);
    const qint64 now = QDateTime::currentSecsSinceEpoch();
    if (!issuedOk || !expiresOk || issued <= 0 || expires <= issued || now < issued || now > expires) {
        if (error) *error = "信任清单已过期、尚未生效或时间字段无效。";
        return false;
    }
    return true;
}

} // namespace

QString supportDiskManifestFormat() {
    return QString::fromLatin1(kManifestFormat);
}

QString supportDiskPublicKeyId() {
    return QString::fromLatin1(kSupportPublicKeyId);
}

QString authorizationPublicKeyId() {
    return QString::fromLatin1(kAuthorizationPublicKeyId);
}

QString linuxSupportDiskPublicKeyId() {
    return QString::fromLatin1(kLinuxSupportPublicKeyId);
}

QString macosSupportDiskPublicKeyId() {
    return QString::fromLatin1(kMacosSupportPublicKeyId);
}

bool verifyEd25519(const QByteArray& publicKeyBase64,
                   const QByteArray& message,
                   const QByteArray& signature,
                   QString* error) {
    const QByteArray publicKey = QByteArray::fromBase64(publicKeyBase64);
    if (publicKey.size() != 32 || signature.size() != 64) {
        if (error) *error = "Ed25519 公钥或签名长度无效。";
        return false;
    }
    EVP_PKEY* key = EVP_PKEY_new_raw_public_key(
        EVP_PKEY_ED25519, nullptr,
        reinterpret_cast<const unsigned char*>(publicKey.constData()),
        static_cast<size_t>(publicKey.size()));
    if (!key) {
        if (error) *error = "无法初始化 Ed25519 公钥。";
        return false;
    }
    EVP_MD_CTX* context = EVP_MD_CTX_new();
    const bool initialized = context
        && EVP_DigestVerifyInit(context, nullptr, nullptr, nullptr, key) == 1;
    const bool verified = initialized
        && EVP_DigestVerify(
               context,
               reinterpret_cast<const unsigned char*>(signature.constData()),
               static_cast<size_t>(signature.size()),
               reinterpret_cast<const unsigned char*>(message.constData()),
               static_cast<size_t>(message.size())) == 1;
    EVP_MD_CTX_free(context);
    EVP_PKEY_free(key);
    if (!verified && error) *error = "Ed25519 签名验证失败。";
    return verified;
}

SupportDiskTrustResult verifySupportDiskManifest(const QString& manifestPath,
                                                 const QString& payloadPath) {
    SupportDiskTrustResult result;
    QJsonObject manifest;
    if (!readJson(manifestPath, &manifest, &result.reason)) {
        return result;
    }
    result.product = requiredString(manifest, "product");
    result.version = requiredString(manifest, "version");
    result.source = requiredString(manifest, "source");
    result.payloadSha256 = requiredString(manifest, "sha256").toLower();
    const QString payloadName = QFileInfo(payloadPath).fileName();
    const bool linuxSupportDisk = result.product == "VirtualWorld Linux Support Disk";
    const bool macosSupportDisk = result.product == "VirtualWorld macOS Support Disk";
    const QString expectedPayloadName = linuxSupportDisk
        ? QStringLiteral("VirtualWorld-LinuxSupportDisk.qcow2")
        : macosSupportDisk
            ? QStringLiteral("VirtualWorld-macOSSupportDisk.qcow2")
            : QStringLiteral("VirtualWorld-RecoveryGuard.qcow2");
    const QByteArray publicKey = linuxSupportDisk
        ? QByteArray(kLinuxSupportPublicKey)
        : macosSupportDisk
            ? QByteArray(kMacosSupportPublicKey)
            : QByteArray(kSupportPublicKey);
    if (requiredString(manifest, "format") != kManifestFormat
        || (result.product != "VirtualWorld Support Disk"
            && result.product != "VirtualWorld Linux Support Disk"
            && result.product != "VirtualWorld macOS Support Disk")
        || result.source != "official"
        || result.version.isEmpty()
        || !isSha256(result.payloadSha256)
        || requiredString(manifest, "payload") != payloadName
        || payloadName != expectedPayloadName) {
        result.reason = "Support Disk 清单字段、产品标识、来源或载荷名称不可信。";
        return result;
    }
    if (!verifyTimeWindow(manifest, &result.reason)) {
        return result;
    }
    const QByteArray signature = QByteArray::fromBase64(
        requiredString(manifest, "signature").toLatin1());
    if (!verifyEd25519(publicKey,
                       canonicalManifest(manifest),
                       signature,
                       &result.reason)) {
        return result;
    }
    QFile payload(payloadPath);
    if (!payload.open(QIODevice::ReadOnly)) {
        result.reason = "无法读取 Support Disk 载荷：" + payload.errorString();
        return result;
    }
    const QByteArray digest = QCryptographicHash::hash(
        payload.readAll(), QCryptographicHash::Sha256).toHex().toLower();
    if (digest != result.payloadSha256.toLatin1()) {
        result.reason = "Support Disk 载荷 SHA-256 与签名清单不一致。";
        return result;
    }
    result.trusted = true;
    result.reason = "Support Disk 已通过来源、时间、签名和 SHA-256 校验。";
    return result;
}

bool verifyHighRiskAuthorization(const QJsonObject& token,
                                 const QString& operation,
                                 const QString& resourceId,
                                 QString* error) {
    if (requiredString(token, "format") != "virtualworld-authorization-v1"
        || requiredString(token, "operation") != operation
        || requiredString(token, "resource") != resourceId
        || requiredString(token, "key_id") != kAuthorizationPublicKeyId) {
        if (error) *error = "授权令牌的操作、资源或密钥标识不匹配。";
        return false;
    }
    if (!verifyTimeWindow(token, error)) {
        return false;
    }
    const QByteArray signature = QByteArray::fromBase64(
        requiredString(token, "signature").toLatin1());
    if (!verifyEd25519(kAuthorizationPublicKey,
                       canonicalAuthorization(token),
                       signature,
                       error)) {
        return false;
    }
    return true;
}

bool authorizeHighRiskOperation(const QJsonObject& token,
                                const QString& operation,
                                const QString& resourceId,
                                bool explicitUserConfirmation,
                                QString* error) {
    if (!explicitUserConfirmation) {
        if (error) *error = "高风险操作必须经过用户明确确认。";
        return false;
    }
    return verifyHighRiskAuthorization(token, operation, resourceId, error);
}

} // namespace vw
