# ci/virtualization/conformance_guard.py
#
# Runtime guard for the CI "read-only conformance" step. It validates a command
# against the shared whitelist (matrix.is_allowed_conformance_command) and, only
# if every token is allowed, optionally executes it. Any token that could boot a
# guest VM (e.g. --start) is rejected outright. This is the enforcement half of
# the honesty rule: CI conformance is read-only and never starts a VM.
#
# Usage:
#   python3 conformance_guard.py -- VirtualWorld --check --json
#   python3 conformance_guard.py --exec -- VirtualWorld --list-backends --json
#
# Exit codes:
#   0  command allowed (and executed, with --exec)
#   2  command rejected by the whitelist
#   3  validation error / usage

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matrix  # noqa: E402


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    do_exec = False
    if "--exec" in argv:
        do_exec = True
        argv = [a for a in argv if a != "--exec"]

    # Support an optional "--" separator before the real command.
    if argv and argv[0] == "--":
        argv = argv[1:]

    if not argv:
        print("usage: conformance_guard.py [--exec] -- <command...>", file=sys.stderr)
        return 3

    ok, reason = matrix.is_allowed_conformance_command(argv)
    if not ok:
        print("conformance_guard: REJECTED - %s" % reason, file=sys.stderr)
        print("conformance_guard: refusing to run VM-launch / non-whitelisted "
              "command in CI.", file=sys.stderr)
        return 2

    print("conformance_guard: allowed -> %s" % " ".join(argv))
    if do_exec:
        # Replace the current process with the validated command.
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            print("conformance_guard: exec failed - %s" % exc, file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
