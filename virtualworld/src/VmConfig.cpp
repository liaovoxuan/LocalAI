#include "VmConfig.hpp"

#include <QMap>
#include <QRegularExpression>
#include <QtGlobal>

namespace vw {

QString normalizeArchitecture(QString value) {
    QString text = value.trimmed().toLower().replace("-", "_").replace(" ", "");
    static const QMap<QString, QString> aliases = {
        {"amd64", "x86_64"},
        {"x64", "x86_64"},
        {"x86", "i386"},
        {"i686", "i386"},
        {"arm64", "aarch64"},
        {"powerpc", "ppc"},
        {"ppc32", "ppc"},
        {"powerpc64", "ppc64"},
        {"ppc64le", "ppc64"},
        {"68k", "m68k"},
        {"68000", "m68k"},
        {"m68000", "m68k"},
        {"riscv64gc", "riscv64"},
    };
    if (text.isEmpty()) {
        return "x86_64";
    }
    return aliases.value(text, text);
}

QString defaultMachineForArch(const QString& archValue) {
    const QString arch = normalizeArchitecture(archValue);
    if (arch == "aarch64" || arch == "arm" || arch == "riscv64") {
        return "virt";
    }
    if (arch == "i386") {
        return "pc";
    }
    if (arch == "ppc") {
        return "mac99";
    }
    if (arch == "ppc64") {
        return "pseries";
    }
    if (arch == "m68k") {
        return "q800";
    }
    return "q35";
}

QString machineBase(const QString& machine) {
    return machine.split(",", Qt::SkipEmptyParts).value(0).trimmed();
}

QString executableForArch(const QString& archValue) {
    const QString arch = normalizeArchitecture(archValue);
    if (arch == "x86_64") {
        return "qemu-system-x86_64";
    }
    if (arch == "i386") {
        return "qemu-system-i386";
    }
    if (arch == "aarch64") {
        return "qemu-system-aarch64";
    }
    if (arch == "arm") {
        return "qemu-system-arm";
    }
    if (arch == "ppc") {
        return "qemu-system-ppc";
    }
    if (arch == "ppc64") {
        return "qemu-system-ppc64";
    }
    if (arch == "riscv64") {
        return "qemu-system-riscv64";
    }
    if (arch == "m68k") {
        return "qemu-system-m68k";
    }
    return "qemu-system-" + arch;
}

QString shellQuote(const QString& value) {
    if (value.isEmpty()) {
        return "''";
    }
    static const QRegularExpression safe("^[A-Za-z0-9_@%+=:,./-]+$");
    if (safe.match(value).hasMatch()) {
        return value;
    }
    QString escaped = value;
    escaped.replace("'", "'\"'\"'");
    return "'" + escaped + "'";
}

QString xmlEscape(const QString& value) {
    QString out = value;
    out.replace("&", "&amp;");
    out.replace("<", "&lt;");
    out.replace(">", "&gt;");
    out.replace("\"", "&quot;");
    out.replace("'", "&apos;");
    return out;
}

QStringList splitOptions(const QString& value) {
    QStringList parts;
    QString buffer;
    QChar quote;
    bool escaped = false;
    for (const QChar ch : value) {
        if (escaped) {
            buffer += ch;
            escaped = false;
            continue;
        }
        if (ch == '\\') {
            buffer += ch;
            escaped = true;
            continue;
        }
        if (!quote.isNull()) {
            buffer += ch;
            if (ch == quote) {
                quote = QChar();
            }
            continue;
        }
        if (ch == '\'' || ch == '"') {
            quote = ch;
            buffer += ch;
            continue;
        }
        if (ch == ',') {
            const QString item = buffer.trimmed();
            if (!item.isEmpty()) {
                parts << item;
            }
            buffer.clear();
            continue;
        }
        buffer += ch;
    }
    const QString item = buffer.trimmed();
    if (!item.isEmpty()) {
        parts << item;
    }
    return parts;
}

QMap<QString, QString> parseOptions(const QString& value) {
    QMap<QString, QString> out;
    QString lastKey;
    for (const QString& part : splitOptions(value)) {
        const int index = part.indexOf('=');
        if (index > 0) {
            lastKey = part.left(index).trimmed();
            QString val = part.mid(index + 1).trimmed();
            if (val.size() >= 2 && ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith('\'') && val.endsWith('\'')))) {
                val = val.mid(1, val.size() - 2);
            }
            out[lastKey] = val;
        } else if (!lastKey.isEmpty()) {
            out[lastKey] += "," + part.trimmed();
        }
    }
    return out;
}

int parseMemoryMb(const QString& value, int fallback) {
    QString text = value.trimmed().toLower();
    bool ok = false;
    double number = 0;
    if (text.endsWith("gb")) {
        number = text.left(text.size() - 2).toDouble(&ok);
        return ok ? int(number * 1024) : fallback;
    }
    if (text.endsWith("g")) {
        number = text.left(text.size() - 1).toDouble(&ok);
        return ok ? int(number * 1024) : fallback;
    }
    if (text.endsWith("mb")) {
        number = text.left(text.size() - 2).toDouble(&ok);
        return ok ? int(number) : fallback;
    }
    if (text.endsWith("m")) {
        number = text.left(text.size() - 1).toDouble(&ok);
        return ok ? int(number) : fallback;
    }
    if (text.endsWith("k")) {
        number = text.left(text.size() - 1).toDouble(&ok);
        return ok ? qMax(1, int(number / 1024)) : fallback;
    }
    number = text.toDouble(&ok);
    if (!ok) {
        return fallback;
    }
    if (number > 1024 * 1024) {
        return int(number / 1024 / 1024);
    }
    if (number > 1024 * 64) {
        return int(number / 1024);
    }
    return int(number);
}

int parseSmp(const QString& value, int fallback) {
    bool ok = false;
    int cores = value.toInt(&ok);
    if (ok) {
        return qMax(1, cores);
    }
    const auto opts = parseOptions(value);
    for (const QString& key : {"cpus", "cores"}) {
        if (opts.contains(key)) {
            cores = opts.value(key).toInt(&ok);
            if (ok) {
                return qMax(1, cores);
            }
        }
    }
    return fallback;
}

} // namespace vw
