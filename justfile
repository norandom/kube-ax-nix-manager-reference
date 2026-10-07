# kube-ax operator tasks. `just` itself comes from the uv venv: run `uv run just <recipe>`.
set shell := ["bash", "-euo", "pipefail", "-c"]

pyinfra := "uv run pyinfra inventory.py"

# List recipes
default:
    @just --list

# Create/refresh the project venv (.venv) from uv.lock
setup:
    uv sync

# Unit tests + lint
test:
    uv run pytest -q
    uv run ruff check .
    uv run ruff format --check .

# All pre-commit linters on every file (ruff, shellcheck, yamllint, markdownlint, gitleaks, go, ...)
lint:
    uv run pre-commit run --all-files

# Regenerate nix/fixtures/*.json from lib/nodeconfig.py
fixtures:
    uv run python scripts/gen_fixtures.py

# Evaluate every NixOS configuration in the podman Nix container (no local Nix needed)
nix-check:
    scripts/nix-check.sh

# Read-only: print facts + the node.json HOST would get. USER=nixos while on the ISO.
probe HOST USER="nixos":
    {{pyinfra}} deploys/probe.py --limit {{HOST}} --data ssh_user={{USER}} -y

# --- later tasks (spec: .kiro/specs/k3s-nixos-cluster/tasks.md) ---

# Install NixOS from the live ISO. WIPE must equal the disk path to allow wiping (task 6)
install HOST WIPE="" *ARGS="":
    {{pyinfra}} deploys/install.py --limit {{HOST}} --data ssh_user=nixos \
        {{ if WIPE != "" { "--data wipe_disk=" + WIPE } else { "" } }} {{ARGS}}

# Enable k3s: servers first, then agents one at a time (task 7)
k3s HOST="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{HOST}}" ]; then
        {{pyinfra}} deploys/k3s.py --limit {{HOST}} -y
    else
        {{pyinfra}} deploys/k3s.py --limit servers -y
        if uv run python -c 'import sys, inventory; sys.exit(0 if inventory.agents else 1)'; then
            {{pyinfra}} deploys/k3s.py --limit agents --serial -y
        fi
    fi
    just kubeconfig

# Fetch kubeconfig to kubeconfig/<cluster>.yaml (local only, gitignored)
kubeconfig:
    {{pyinfra}} deploys/kubeconfig.py --limit servers -y

# Merge kubeconfig/NAME.yaml into ~/.kube/config (kube-ax = admin, ax-deployer = AX workflows)
kube-local NAME="kube-ax":
    scripts/kube-local.sh {{NAME}}

# kubectl against the committed kubeconfig
kubectl *ARGS:
    KUBECONFIG=kubeconfig/kube-ax.yaml kubectl {{ARGS}}

# OpenObserve collector stack (cert-manager, OTel operator, o2c chart, sized for small nodes)
openobserve:
    scripts/openobserve.sh

# Gitea Actions runners (pods + DinD) for code.because-security.com
gitea-runner:
    scripts/gitea-runner.sh

# Install Agent Substrate + AX (task 10)
platform *STEPS:
    scripts/platform.sh {{STEPS}}

# Restricted kubeconfig for submitting AX workflows -> kubeconfig/ax-deployer.yaml
ax-deployer:
    scripts/ax-deployer.sh

# Copy the AX handoff brief (incl. ax-deployer credentials) to the clipboard for another Claude Code
ax-handoff:
    scripts/ax-guide-sync.sh >/dev/null
    wl-copy < .cache/handoff/ax-handoff.md
    @echo ".cache/handoff/ax-handoff.md copied to clipboard ($(wc -l < .cache/handoff/ax-handoff.md) lines, contains the ax-deployer token)"

# One AX task's output and exit code (from the worker logs; works as ax-deployer)
ax-logs TASK SINCE="2h":
    scripts/ax-logs.sh {{TASK}} {{SINCE}}

# Fan one workflow out into COUNT AX tasks spread across both nodes
ax-fanout NAME COUNT IMAGE *CMD:
    scripts/ax-fanout.sh {{NAME}} {{COUNT}} {{IMAGE}} -- {{CMD}}

# Cluster health, spread/ingress tests, tuning assertions (task 9)
verify:
    {{pyinfra}} deploys/verify.py -y

# Per-layer memory/CPU report to reports/ (task 10.7)
footprint:
    @echo "not implemented yet: task 10.7" && exit 1

# install -> k3s -> verify for one new node
add-node HOST WIPE="":
    just install {{HOST}} {{WIPE}}
    just k3s {{HOST}}
    just verify

# Publish the current tree to the public repo as one squashed commit (scans for secrets first)
publish MSG="":
    scripts/publish.sh "{{MSG}}"
