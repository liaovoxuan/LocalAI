#!/usr/bin/env python3
import os
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "runtime" / "llama.cpp" / "models"
DEFAULT_MODEL_NAME = os.environ.get("LOCALAI_LLAMA_CPP_MODEL", "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")
MODEL_URLS = {
    "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf": "https://huggingface.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
    "Qwen2.5-3B-Instruct-Q4_K_M.gguf": "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf",
}


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    done = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "LocalAI-build"}
    if done:
        headers["Range"] = f"bytes={done}-"
    request = Request(url, headers=headers)
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        response_cm = urlopen(request, timeout=60, context=context)
    except Exception:
        curl = shutil.which("curl")
        if not curl:
            raise
        command = [
            curl,
            "-L",
            "--fail",
            "--retry",
            "3",
            "--connect-timeout",
            "30",
            "-C",
            "-",
            "-o",
            str(partial),
            url,
        ]
        subprocess.check_call(command)
        partial.replace(target)
        return

    with response_cm as response:
        mode = "ab" if done and response.status == 206 else "wb"
        if mode == "wb":
            done = 0
        total = int(response.headers.get("Content-Length", "0") or 0) + done
        last = time.time()
        with partial.open(mode + "") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if time.time() - last > 1:
                    if total:
                        print(f"{target.name}: {done * 100 // total}%")
                    else:
                        print(f"{target.name}: {done // (1024 * 1024)} MB")
                    last = time.time()
    partial.replace(target)


def main():
    url = MODEL_URLS.get(DEFAULT_MODEL_NAME)
    if not url:
        raise SystemExit(f"No default llama.cpp model URL for {DEFAULT_MODEL_NAME}")
    target = MODEL_DIR / DEFAULT_MODEL_NAME
    if target.exists() and target.stat().st_size > 1024 * 1024:
        print(f"default llama.cpp model already exists: {target}")
        return
    download(url, target)
    print(f"default llama.cpp model: {target}")


if __name__ == "__main__":
    main()
