# C++ Migration Status

## Scope

The application layer is being migrated from Python to C++/Qt.

Do not rewrite or delete Python files under `virtualworld/qemu-master/` as part of the LocalAI migration. Those files are upstream QEMU build, test, documentation, and generator tools. Replacing them would break the bundled QEMU source tree.

## Native C++ Applications Added

- `native_ai/LocalAI_Native`
- `native_ai/CloudAI_Native`

Current native coverage:

- Qt Widgets chat shell
- Config read/write
- Shared language setting basics
- Light/dark theme basics
- Ollama chat endpoint
- LM Studio/OpenAI-compatible chat endpoint
- CloudAI-compatible endpoint path
- API key masking in UI input

## Python Application Files Still To Migrate

- `local_ai.py`
- `local_ai_se.py`
- `cloud_ai.py`
- `version_manager.py`
- first-party plugin code under `plugins/`

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
- packaging specs/workflows switched from Python entry points to C++ binaries

## Build

```bash
cmake -S native_ai -B build/native_ai -DCMAKE_BUILD_TYPE=Release
cmake --build build/native_ai --config Release
```

Outputs:

- `build/native_ai/LocalAI_Native`
- `build/native_ai/CloudAI_Native`
