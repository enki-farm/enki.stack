#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/k8s/cloudflared"
cp -R "$ROOT_DIR/enki.infra/aks" "$ROOT_DIR/enki.infra/k3s" "$ROOT_DIR/enki.infra/docs" "$TEST_DIR/"
cp "$ROOT_DIR/enki.infra/README.md" "$ROOT_DIR/enki.infra/LICENSE" "$ROOT_DIR/enki.infra/.gitignore" "$TEST_DIR/"
for file in namespace.yaml deployment.yaml kustomization.yaml .env.example; do
  cp "$ROOT_DIR/enki.infra/k8s/cloudflared/$file" "$TEST_DIR/k8s/cloudflared/$file"
done
cd "$TEST_DIR"
for script in aks/create-aks.sh k3s/install-k3s.sh; do
  bash -n "$script"
  [[ -x "$script" ]]
  bash "$script" --help > /dev/null
done
bash k3s/install-k3s.sh --dry-run > /dev/null
cp k8s/cloudflared/.env.example k8s/cloudflared/.env
if command -v kustomize >/dev/null 2>&1; then
  kustomize build k8s/cloudflared > rendered.yaml
else
  kubectl kustomize k8s/cloudflared > rendered.yaml
fi
for resource in Namespace Deployment Secret; do
  command grep -q "kind: $resource" rendered.yaml
done
git init -q .
git check-ignore -q k8s/cloudflared/.env
! git check-ignore -q k8s/cloudflared/.env.example
git check-ignore -q kubeconfig
command grep -q './aks/create-aks.sh' aks/create-aks.sh
command grep -q './k3s/install-k3s.sh' k3s/install-k3s.sh
printf 'PASS: standalone provisioning paths, executable modes, tunnel rendering and credential ignores\n'