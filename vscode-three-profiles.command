#!/bin/zsh
# Compatibility name: use the launcher with the requested stable/Code 2/
# Insiders behavior.
exec "${0:A:h}/vscode-launch-verified.command" "$@"
