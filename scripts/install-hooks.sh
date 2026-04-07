#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"

git -C "$repo_root" config core.hooksPath .githooks
chmod +x "$repo_root/.githooks/pre-commit" "$repo_root/.githooks/pre-push"

printf '%s\n' 'Configured git core.hooksPath to .githooks'
printf '%s\n' 'Installed tracked hooks:'
printf '%s\n' '  - .githooks/pre-commit'
printf '%s\n' '  - .githooks/pre-push'
