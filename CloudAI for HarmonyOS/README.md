# CloudAI for HarmonyOS

This folder contains a first C++ translation of CloudAI's non-Tkinter core for HarmonyOS/OpenHarmony style native integration.

It intentionally does not include `module.json5`, `app.json5` or signing metadata yet. Those files need the final bundle ID, module name, permissions and signing profile.

## What Is Included

- Shared LocalAI language config reading.
- CloudAI provider config reading and masked API key storage.
- OpenAI-compatible chat request payload generation.
- Usage endpoint probing.
- File attachment text passthrough.
- Basic content moderation fallback.
- A small CLI entry for desktop smoke testing.
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

With libcurl:

```bash
cmake -S . -B build -DCLOUDAI_USE_LIBCURL=ON
cmake --build build
```

HarmonyOS integration should link `src/CloudAIHarmony.cpp` into the native module and provide either:

- a libcurl-capable build, or
- a custom `IHttpClient` implementation backed by HarmonyOS networking APIs.

