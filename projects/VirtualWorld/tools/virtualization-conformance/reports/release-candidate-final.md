# VirtualWorld release candidate — final independent acceptance

- Generated: `2026-10-06T02:52:12+00:00`
- Report schema: `virtualworld.release-candidate-final/1`
- **Verdict: `blocked`**
- independent black-box acceptance; no build, no rebuild, no product modification; only --check/--list-backends/--json are issued to the candidate and no VM is started

## Blocked

- the release gate published release-blocked.json
- Marker: `build-release-candidate/release-blocked.json`


### Blocked-marker claims (independently re-derived)

- Blocked marker schema: `virtualworld.release-blocked/1` (expected `virtualworld.release-blocked/1`)
- Blocked marker SHA-256: `a8297ca4fb7f287ef8ae9f04c63cf8e36d5691bc1e1a3e3c4d49793b043627a3`
- Gate result / error code: **BLOCKED** / `PLATFORM_FREEZE_HASH_MISMATCH`
- Recorded freeze-marker: `build-mac-harmony-chaos/.platform-freeze-ready`
- Hash claims checked: 8, confirmed: 8, refuted: 0, files missing: 0
- Evidence authenticated: **True**
- Disputed file: `docs/harmonyos-virtualization.md`
  - recomputed on disk: `1d42259dbd464dbb4ef68a8c6274853fc485cd7f93f21087db57b321c824bbd5`
  - mismatch reproduced independently: **True**

**Gate evidence:**

> docs/harmonyos-virtualization.md on disk (sha256=1d42259dbd464dbb4ef68a8c6274853fc485cd7f93f21087db57b321c824bbd5) does not match the freeze marker's recorded sha256 (3c4d0462c3cb502a7677d0262b9c0f4e2963d2ab86075c1eba8a5e87ed41013b). The marker asserts these 8 platform files are NOT modified further, but disk state is inconsistent with that claim, so the freeze is untrustworthy as a stable base for the final Release Candidate build. Per the gate rule, this is an immediate BLOCK. No Release Candidate build, CTest, Debian real-hardware smoke, or release-ready.json was produced.

