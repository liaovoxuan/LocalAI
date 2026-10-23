# C++ Migration Status

## Scope

The application layer is being migrated from Python to C++/Qt.

Do not rewrite or delete upstream files under `projects/VirtualWorld/qemu-source/` as part of the LocalAI migration. Replacing them would break the bundled QEMU source tree.

## Native C++ Applications Added

- `shared/native/native_ai/LocalAI_Native`
- `shared/native/native_ai/CloudAI_Native`

Current native coverage:

- Qt Widgets chat shell with background provider requests
- Shared LocalAI/CloudAI configuration migration and atomic writes
- Separate API-key store compatible with CloudAI's existing `v1:` secret format
- Ollama, llama.cpp, LM Studio, OpenAI-compatible, OpenAI, DeepSeek, OpenRouter, and SiliconFlow endpoints
- Light/dark/auto theme basics and macOS application bundles
- CTest coverage for C++ configuration and secret persistence

## Legacy Python Sources

- `projects/LocalAI/desktop/local_ai.py`
- `projects/LocalAI-SE/desktop/local_ai_se.py`
- `projects/CloudAI/desktop/cloud_ai.py`
- `tooling/python/version_manager.py`
- first-party plugin code under `projects/LocalAI/desktop/plugins/`

The native C++ executables are now the supported application entry points for
LocalAI, LocalAI SE, and CloudAI. The Python sources remain in the repository
as a compatibility reference while feature parity and non-C++ packaging are
retired incrementally.

The transitional Python runtime is enabled only by
`LOCALAI_ENABLE_PYTHON_RUNTIME` and is launched through the C++
`PythonMigrationTranslator` bridge. HarmonyOS/OHOS, LoongArch, RISC-V, and
Windows on ARM force the Python and C++ provider layers to use llama.cpp. If a
native Python runtime is not packaged for the target, the application falls back
to its C++ UI. A platform is not considered released until it has been built
with that platform's SDK and passed tests on target hardware.

Cross-platform runtime packages provide their platform-native PyInstaller (or
equivalent) output through `LOCALAI_PYTHON_RUNTIME_ROOT`. The C++ bridge uses
Unicode process creation on Windows and direct POSIX execution on macOS, Linux,
HarmonyOS/OHOS, LoongArch, and RISC-V targets. `PythonTranslatorBridgeTest`
validates process startup and compatibility-policy propagation in every build.

## Remaining Feature Migration

- Full multilingual string table
- First-run wizard parity
- model/provider settings parity
- web search and source filtering
- RAG/file reading
- image/document import
- chat history export/import
- wallpaper and custom background rendering
- QEMU/VirtualWorld plugin bridge integration
- Windows/Linux distributable packaging and full app feature parity

## Build

```bash
cmake -S shared/native/native_ai -B build/native_ai -DCMAKE_BUILD_TYPE=Release
cmake --build build/native_ai --config Release
ctest --test-dir build/native_ai --output-on-failure
```

Outputs:

- `build/native_ai/LocalAI_Native`
- `build/native_ai/CloudAI_Native`
