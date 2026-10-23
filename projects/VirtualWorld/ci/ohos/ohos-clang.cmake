# CMake toolchain for OpenHarmony aarch64 (musl) using the SDK clang wrappers.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

# Use the wrapper scripts; they already provide --target/--sysroot/-D__MUSL__.
set(CMAKE_C_COMPILER   "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/aarch64-unknown-linux-ohos-clang" CACHE FILEPATH "OHOS clang")
set(CMAKE_CXX_COMPILER "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/aarch64-unknown-linux-ohos-clang++" CACHE FILEPATH "OHOS clang++")
set(CMAKE_AR   "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/llvm-ar"   CACHE FILEPATH "llvm-ar")
set(CMAKE_RANLIB "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/llvm-ranlib" CACHE FILEPATH "llvm-ranlib")
set(CMAKE_STRIP "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/llvm-strip" CACHE FILEPATH "llvm-strip")
set(CMAKE_NM   "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/llvm-nm"   CACHE FILEPATH "llvm-nm")
set(CMAKE_SYSROOT "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/sysroot")

# Do NOT let CMake search host (Darwin/Homebrew) paths.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(CMAKE_FIND_ROOT_PATH "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/sysroot" "/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-qemu-ohos-deps/prefix")

# pkg-config: only the private prefix (no Homebrew/Darwin contamination).
set(PKG_CONFIG_EXECUTABLE "/opt/homebrew/bin/pkg-config" CACHE FILEPATH "host pkg-config")
set(ENV{PKG_CONFIG_LIBDIR} "/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-qemu-ohos-deps/prefix/lib/pkgconfig")
set(ENV{PKG_CONFIG_PATH}   "")
