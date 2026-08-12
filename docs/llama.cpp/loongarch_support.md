# LoongArch llama.cpp support

LocalAI uses the upstream llama.cpp LoongArch support already present in
`third_party/llama.cpp`.

Relevant upstream files:

- `ggml/src/ggml-cpu/arch/loongarch/quants.c`
- `ggml/src/ggml-cpu/CMakeLists.txt`
- `ggml/CMakeLists.txt`

The build enables:

- `GGML_CPU=ON`
- `GGML_OPENMP=ON`
- `GGML_LASX=ON`
- `GGML_LSX=ON`

Vulkan is disabled for LoongArch builds because the current LocalAI package
uses the upstream CPU backend path for better first-run compatibility.

Runtime binaries are packaged under:

`runtime/llama.cpp/linux/loongarch64/bin`

The downloaded `projX-la-llama.cpp` material is archived only as background
reference. It is not a source patch.
