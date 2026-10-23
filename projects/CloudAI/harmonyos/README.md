# CloudAI for HarmonyOS

This folder contains the HarmonyOS project wrapper for the shared CloudAI C++ core in `../source/cpp`.

The ArkTS shell imports `libcloudai_harmony.so`. Its NAPI bridge uses the shared C++ core for provider configuration, API-key storage, request payload generation, moderation and response parsing. ArkTS owns the HarmonyOS HTTP lifecycle and passes the response back to the native parser, so device networking is not replaced by a desktop-only libcurl dependency.

## What Is Included

- Shared LocalAI language config reading.
- CloudAI provider config reading and masked API key storage.
- OpenAI-compatible chat request payload generation.
- Usage endpoint probing.
- File attachment text passthrough (256 KiB read limit, 12,000 character context limit).
- Basic content moderation fallback.
- A small CLI entry for desktop smoke testing.
- A NAPI entry used by the ArkTS page (`getStatus`, `setApiKey`, `configureProvider`, `prepareRequest`, `responseText`).
- Built-in OpenAI, OpenRouter, DeepSeek and SiliconFlow choices plus a custom OpenAI-compatible provider accepting a user-supplied HTTP(S) URL and model name.
- Agent mode with the shared JSON action protocol. HarmonyOS executes read-only text-file actions inside `filesDir`; shell, external URL and write actions are rejected explicitly.
- Optional libcurl HTTP backend, enabled with `CLOUDAI_USE_LIBCURL`.

## Expected Config Layout

The HarmonyOS native app should point `CloudAIHarmony::ConfigStore` at the same logical app data directory used by the platform app:

```text
config.json
config/cloudai_config.json
config/cloudai_secrets.json
cloud_chats/
logs/
```

`config.json` keeps the shared LocalAI language field:

```json
{
  "language": "zh_cn",
  "theme": "auto"
}
```

`config/cloudai_config.json` keeps the public CloudAI provider settings. API keys are stored separately in `cloudai_secrets.json` and should only be displayed as `********`.

## Build Notes

Desktop smoke test:

```bash
cmake -S . -B build
cmake --build build
./build/cloudai_harmony_cli
```

The canonical source and C++ smoke test are also available directly:

```bash
cmake -S ../source/cpp -B ../source/build-cpp
cmake --build ../source/build-cpp
ctest --test-dir ../source/build-cpp --output-on-failure
```

With libcurl:

```bash
cmake -S . -B build -DCLOUDAI_USE_LIBCURL=ON
cmake --build build
```

HarmonyOS integration links `../source/cpp/src/CloudAIHarmony.cpp` and `entry/src/main/cpp/cloudai_napi.cpp` into `libcloudai_harmony.so`. The UI calls the native bridge for request construction and uses `@kit.NetworkKit` for the actual HTTPS request.
