#pragma once

#include "VmConfig.hpp"

namespace vw {

enum class RecoveryDiagnosticKind {
    Configuration,
    Disk,
    GuestSystemDamage,
};

struct RecoveryDiagnostic {
    RecoveryDiagnosticKind kind = RecoveryDiagnosticKind::Configuration;
    QString technicalDetail;
};

RecoveryDiagnostic diagnoseWindowsBootFailure(const VmConfig& config, const QString& startupDetail);
RecoveryDiagnostic diagnoseLinuxBootFailure(const VmConfig& config, const QString& startupDetail);
RecoveryDiagnostic diagnoseMacosBootFailure(const VmConfig& config, const QString& startupDetail);

} // namespace vw
