#pragma once

#include "QemuEngine.hpp"

#include <QString>

namespace vw {

class ImageManager {
public:
    explicit ImageManager(QemuEngine engine = QemuEngine());
    bool createQcow2(const QString& imagePath, qint64 sizeMb, QString* error = nullptr) const;

private:
    QemuEngine engine_;
};

} // namespace vw
