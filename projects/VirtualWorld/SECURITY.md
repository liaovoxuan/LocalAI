# VirtualWorld Security Boundary

## Reporting a vulnerability

Please do not disclose an exploitable vulnerability in a public issue. Contact
the maintainer, 廖博轩 (Liao Boxuan), through a private security-reporting
channel provided by the repository host. Include the affected revision,
platform, reproduction steps, impact, and any suggested mitigation. If no
private channel is configured yet, publish no exploit details until the
maintainer has provided one.

Never attach private keys, credentials, personal VM images, or copyrighted
guest media to a report. Acknowledgement does not guarantee a particular fix or
release date.

VirtualWorld's application layer and Support Disk trust protocol are
maintained separately from the QEMU runtime.

## Support Disk trust

An official Support Disk update must contain a JSON manifest and a payload.
The application accepts it only when all of these checks pass:

1. The manifest format, product name, payload filename, and source marker match.
2. The manifest is inside its issued/expiry time window.
3. The manifest is signed with the official Ed25519 Support Disk key.
4. The payload SHA-256 exactly matches the signed manifest.

Windows, Linux, and macOS Support Disks are separate products. They have
distinct payload filenames and distinct Ed25519 public keys, so a signed
manifest cannot authorize a payload from another product. The macOS Support
Disk is only eligible when the host platform is macOS. Linux guests use the read-only
`linux-recovery-guard` role; it is excluded from normal guest boot order and
is only used by VirtualWorld's recovery/self-check workflow.

The application contains public keys only. Private signing keys are release
infrastructure secrets and must never be committed to this repository.

## macOS IPSW sources

The macOS download flow may use a firmware catalog to discover candidates, but
it accepts only HTTPS URLs whose final host is `apple.com` or a subdomain of
`apple.com`. Redirects to other hosts are rejected. When a catalog supplies a
valid SHA-256 value, the downloaded file must match it before it is attached
to the VM. Manually imported IPSW files remain explicitly unverified.

VirtualWorld has a `Virtualization.framework` backend for supported guests, but
macOS IPSW restore/install is not wired into that backend. The verified IPSW
path is currently a QEMU boot/recovery-media path, not a claim of native
Apple-device restore semantics. Native IPSW restore would require its own
entitlement, lifecycle, and release audit.

The canonical signed fields are:

```text
format
product
version
source
payload
sha256
issued_at
expires_at
```

The command-line verifier is:

```text
VirtualWorld --verify-support-disk <manifest.json> <support-disk>
```

## High-risk operations

Host-impacting operations must use a separate authorization key and a
short-lived token bound to the exact operation and resource. A valid token is
not sufficient by itself: the caller must also obtain explicit user
confirmation. A modified client must not silently elevate an untrusted
Support Disk to trusted status.

This is an application-level supply-chain boundary, not a claim that a user
who controls a machine can never patch a local binary.

## Key rotation

Key rotation requires a new signed release and a migration policy. Do not
place private keys, passwords, or signing scripts in the source tree.

## Supported versions

Until the project publishes a stable release policy, security fixes target the
current development branch. Older snapshots and third-party builds may need to
be upgraded before a fix can be applied.
