# GitHub synchronization

The repository uses HTTPS with the credentials configured by GitHub CLI:

```sh
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
```

On this Mac, the system HTTPS proxy is `127.0.0.1:1082`. Command-line Git
and Git LFS needed an explicit repository proxy setting to improve upload speed:

```sh
git config --local http.proxy http://127.0.0.1:1082
```

If that local proxy is disabled or its port changes, update this setting or
remove it with `git config --local --unset http.proxy`.

## Knowledge database

`localai_knowledge.db.gz` is a lossless gzip archive of the workspace database.
The raw database is larger than GitHub's 100 MiB ordinary-file limit. It remains
available locally and is excluded from Git; no Git LFS download is required.
After cloning, restore the database before starting LocalAI:

```sh
gzip -dk localai_knowledge.db.gz
git submodule update --init --recursive
```

The database snapshot SHA-256 is
`742a20e5ece20912e54aa4d914af01cb66d04cdc49c2d49eabb8bac7340459e6`.
The gzip archive was verified to restore the database byte for byte.
After changing the database, refresh the archive before committing:

```sh
gzip -c localai_knowledge.db > localai_knowledge.db.gz
```

Build output, local provider credentials, search caches and local assistant state
are excluded. Local files remain on disk.

## Local changes in upstream source repositories

The following checked-in binary-capable patches preserve workspace modifications
without requiring write access to the upstream projects. Apply each patch once,
after initializing the submodules at the commits recorded by this repository:

```sh
git -C projects/UTM/source apply ../patches/local-workspace.patch
git -C projects/VirtualWorld/qemu-ohos-source apply ../patches/qemu-ohos-local-workspace.patch
```

The patches include changed source files and newly added source files. Local
build directories and sysroots are excluded. The submodules will show local
modifications after the patches are applied; this is expected. Update the patches
when making further changes within these upstream checkouts.

## Uploading further changes

```sh
git add -A
git diff --cached --stat
git commit -m "Describe the changes"
git push origin main
```

Use `git status --ignored` to inspect excluded files. Ignoring a path does not untrack an already committed
or staged file; use `git rm --cached -- PATH` when that is needed, keeping the
local file intact. Never commit real API keys into example configuration files.
