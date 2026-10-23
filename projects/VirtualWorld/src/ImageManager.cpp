#include "ImageManager.hpp"

#include <utility>

namespace vw {

ImageManager::ImageManager(QemuEngine engine)
    : engine_(std::move(engine)) {}

bool ImageManager::createQcow2(const QString& imagePath, qint64 sizeMb, QString* error) const {
    return engine_.createQcow2(imagePath, sizeMb, error);
}

bool ImageManager::createClassicMacQcow2(const QString& imagePath, qint64 sizeMb, QString* error) const {
    return engine_.createClassicMacQcow2(imagePath, sizeMb, error);
}

bool ImageManager::isQcow2Blank(const QString& imagePath, bool* blank, QString* error) const {
    return engine_.isQcow2Blank(imagePath, blank, error);
}

} // namespace vw
