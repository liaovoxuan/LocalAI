#include "ImageManager.hpp"

#include <utility>

namespace vw {

ImageManager::ImageManager(QemuEngine engine)
    : engine_(std::move(engine)) {}

bool ImageManager::createQcow2(const QString& imagePath, qint64 sizeMb, QString* error) const {
    return engine_.createQcow2(imagePath, sizeMb, error);
}

} // namespace vw
