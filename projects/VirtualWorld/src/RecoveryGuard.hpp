#pragma once

#include "VmConfig.hpp"

namespace vw {

bool isSupportedWindowsGuest(const VmConfig& config);
bool isSupportedLinuxGuest(const VmConfig& config);
bool isSupportedMacosGuest(const VmConfig& config);
bool isRecoveryGuardDisk(const DiskConfig& disk);
bool isLinuxRecoveryGuardDisk(const DiskConfig& disk);
bool isMacosRecoveryGuardDisk(const DiskConfig& disk);
QString recoveryGuardDiskFileName();
QString sharedRecoveryGuardDiskPath();
QString sharedRecoveryGuardManifestPath();
bool ensureTrustedSharedRecoveryGuardDisk(QString* error = nullptr);
QString linuxRecoveryGuardDiskFileName();
QString sharedLinuxRecoveryGuardDiskPath();
QString sharedLinuxRecoveryGuardManifestPath();
bool ensureTrustedSharedLinuxRecoveryGuardDisk(QString* error = nullptr);
QString macosRecoveryGuardDiskFileName();
QString sharedMacosRecoveryGuardDiskPath();
QString sharedMacosRecoveryGuardManifestPath();
bool ensureTrustedSharedMacosRecoveryGuardDisk(QString* error = nullptr);

} // namespace vw
