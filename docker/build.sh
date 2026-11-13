#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

docker build \
	--tag mat-sandbox:latest \
	--file "$project_root/docker/Dockerfile.sandbox" \
	"$project_root"
