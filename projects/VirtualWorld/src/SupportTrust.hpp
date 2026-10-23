#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QString>

namespace vw {

struct SupportDiskTrustResult {
    bool trusted = false;
    QString reason;
    QString product;
    QString version;
    QString source;
    QString payloadSha256;
};

// Verifies an official Support Disk manifest and the payload it describes.
// The manifest signature covers the canonical fields returned by the parser.
SupportDiskTrustResult verifySupportDiskManifest(const QString& manifestPath,
                                                 const QString& payloadPath);

// Verifies a detached Ed25519 signature using a raw 32-byte public key encoded
// as base64. This generic form is also used by the authorization-token tests.
bool verifyEd25519(const QByteArray& publicKeyBase64,
                   const QByteArray& message,
                   const QByteArray& signature,
                   QString* error = nullptr);

// High-risk host operations require a separately signed, short-lived token and
// an explicit confirmation by the caller. The token is never sufficient alone.
bool verifyHighRiskAuthorization(const QJsonObject& token,
                                 const QString& operation,
                                 const QString& resourceId,
                                 QString* error = nullptr);
bool authorizeHighRiskOperation(const QJsonObject& token,
                                const QString& operation,
                                const QString& resourceId,
                                bool explicitUserConfirmation,
                                QString* error = nullptr);

QString supportDiskManifestFormat();
QString supportDiskPublicKeyId();
QString linuxSupportDiskPublicKeyId();
QString macosSupportDiskPublicKeyId();
QString authorizationPublicKeyId();

} // namespace vw
