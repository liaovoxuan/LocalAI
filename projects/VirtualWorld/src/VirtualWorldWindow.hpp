#pragma once

#include "VMManager.hpp"
#include "VmConfig.hpp"
#include "ImageDownloadManager.hpp"

#include <QDoubleSpinBox>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QPointer>
#include <QWidget>

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QTextEdit;

namespace vw {

class VirtualMachineWindow;

class VirtualWorldWindow : public QWidget {
public:
    explicit VirtualWorldWindow(QWidget* parent = nullptr);

private:
    void buildUi();
    void showEmptyLibrary();
    void setEditorAvailable(bool available);
    VmConfig createVmWithWizard();
    void applyGuestPreset(VmConfig& config) const;
    QString defaultStorageRoot() const;
    QString defaultStorageDirForVm(const QString& name) const;
    QString sanitizeFileName(const QString& name) const;
    QString libraryIndexPath() const;
    void refreshGpuPassthroughChoices();
    bool maybeAskGpuPassthrough(VmConfig& config);
    void loadVmToEditor(const VmConfig& config);
    VmConfig editorToVm() const;
    void refreshVmList();
    void loadSavedLibrary();
    void saveLibraryIndex() const;
    void persistCurrentVm();
    void refreshPreview();
    void refreshMediaList();
    void applyMediaListOrder();
    void removeSelectedMedia();
    void appendIssueText(const QVector<ValidationIssue>& issues);
    void showMessage(const QString& title, const QString& message);
    bool prepareStorageDirectory(QString* path, QString* error) const;
    bool ensureWritableDiskImages(VmConfig& config, QString* error) const;
    bool ensureSystemSoftwareRom(VmConfig& config, QString* error) const;
    bool ensureRecoveryGuardDisk(VmConfig& config, QString* error) const;
    void handleBootFailure(int launchedIndex, VirtualMachineWindow* window, const QString& detail);
    void handleFirstFrameReady(int launchedIndex);

    void newVm();
    void deleteVm();
    void importQemu();
    void importUtm();
    void importVirtualWorld();
    void saveVirtualWorld();
    void exportQemu();
    void exportUtm();
    void startVm();
    void stopVm();
    void addDisk();
    void addCdrom();
    void createDiskImage();
    void downloadImage();
    void switchBootImage();
    int memoryValueMb() const;
    qint64 memoryValueKb() const;
    qint64 sizeValueMb() const;

    VMManager manager_;
    ImageDownloadManager downloads_;
    QVector<VmConfig> vms_;
    QVector<QPointer<VirtualMachineWindow>> vmWindows_;
    int currentIndex_ = -1;
    qint64 runningPid_ = 0;
    bool loadingEditor_ = false;

    QListWidget* vmList_ = nullptr;
    QLabel* emptyStateLabel_ = nullptr;
    QWidget* editorPane_ = nullptr;
    QListWidget* mediaList_ = nullptr;
    QLineEdit* nameEdit_ = nullptr;
    QLineEdit* storageEdit_ = nullptr;
    QComboBox* archCombo_ = nullptr;
    QComboBox* guestOsCombo_ = nullptr;
    QComboBox* machineCombo_ = nullptr;
    QLineEdit* cpuEdit_ = nullptr;
    QDoubleSpinBox* coresSpin_ = nullptr;
    QDoubleSpinBox* memorySpin_ = nullptr;
    QComboBox* memoryUnitCombo_ = nullptr;
    QDoubleSpinBox* diskSizeSpin_ = nullptr;
    QComboBox* diskSizeUnitCombo_ = nullptr;
    QComboBox* diskInterfaceCombo_ = nullptr;
    QComboBox* acceleratorCombo_ = nullptr;
    QCheckBox* networkEnabledCheck_ = nullptr;
    QComboBox* networkModeCombo_ = nullptr;
    QComboBox* networkModelCombo_ = nullptr;
    QLineEdit* networkBridgeEdit_ = nullptr;
    QLineEdit* networkMacEdit_ = nullptr;
    QLineEdit* networkForwardsEdit_ = nullptr;
    QCheckBox* audioEnabledCheck_ = nullptr;
    QComboBox* audioDriverCombo_ = nullptr;
    QComboBox* audioModelCombo_ = nullptr;
    QComboBox* graphicsCombo_ = nullptr;
    QComboBox* inputCombo_ = nullptr;
    QCheckBox* gpuPassthroughCheck_ = nullptr;
    QComboBox* gpuPassthroughCombo_ = nullptr;
    QCheckBox* keepVirtualDisplayCheck_ = nullptr;
    QCheckBox* openGlCheck_ = nullptr;
    QCheckBox* autoResizeCheck_ = nullptr;
    QCheckBox* retinaCheck_ = nullptr;
    QCheckBox* tpmCheck_ = nullptr;
    QLineEdit* tpmSocketEdit_ = nullptr;
    QLineEdit* kernelEdit_ = nullptr;
    QLineEdit* initrdEdit_ = nullptr;
    QLineEdit* appendEdit_ = nullptr;
    QLineEdit* systemSoftwareRomEdit_ = nullptr;
    QLineEdit* systemSoftwareFloppyAEdit_ = nullptr;
    QLineEdit* systemSoftwareFloppyBEdit_ = nullptr;
    QWidget* qemuMediaControls_ = nullptr;
    QWidget* systemSoftwareFloppyWidget_ = nullptr;
    QTextEdit* issueText_ = nullptr;
    QPlainTextEdit* commandPreview_ = nullptr;
};

} // namespace vw
