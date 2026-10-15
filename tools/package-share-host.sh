#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d /tmp/vice-share-package.XXXXXXXX)"
trap 'rm -rf -- "$work"' EXIT
target="$work/vice-share-host"
mkdir -p "$target/vice" "$target/deploy/share-host" "$root/dist"
cp -- "$root/.dockerignore" "$target/"
cp -- "$root/vice/__init__.py" "$root/vice/share_host.py" "$target/vice/"
for file in Dockerfile requirements.txt compose.yaml compose.tunnel.yaml .env.example init_env.py README.md; do
    cp -- "$root/deploy/share-host/$file" "$target/deploy/share-host/"
done
tar -czf "$root/dist/vice-share-host.tar.gz" -C "$work" vice-share-host
echo "$root/dist/vice-share-host.tar.gz"
