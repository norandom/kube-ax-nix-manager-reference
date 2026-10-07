# kube-ax-nix-manager

A small, efficient Kubernetes cluster for **sandboxed agent and compute workloads**, built entirely from
this repo. Two Proxmox VMs run **NixOS + k3s**; on top runs **AX on Agent Substrate**, which executes each
task in a **gVisor sandbox** and spreads tasks across both machines. Everything is reproducible: one command
installs a node from the NixOS ISO, one command joins it to the cluster, one command checks it.

| | |
|---|---|
| Nodes | `kube-01` 10.0.0.63 (control plane + workloads, single entry IP), `kube-02` 10.0.0.58, each 4 vCPU / 8 GiB |
| Stack | NixOS 26.05 · k3s 1.36.4 · Agent Substrate main (v0.3.0-34-g715f2daa) · AX v0.3.1 · OpenObserve telemetry |
| Status | `uv run just verify` passes 46 checks (2026-10-05) |

## Why it's built this way

- **Small machines, real workloads.** Defaults favour memory and CPU efficiency:
  - the base OS holds ~115 MiB;
  - jobs get ~6.7 GiB of each 8 GiB node;
  - heavy jobs are stopped cleanly instead of taking the node down.

  → [How the setup is tuned](docs/optimizations.md)
- **Untrusted code needs a sandbox.** Agents run code nobody reviewed. Each task runs in gVisor, a stand-in
  kernel between the task and the machine, and may only reach public HTTPS.
  → [Sandboxing in plain English (gVisor vs Kata micro-VMs)](docs/sandboxing.md)
- **Nothing configured by hand.** The repo is the source of truth:
  - NixOS describes each node's entire system;
  - pyinfra drives install and upgrades;
  - re-running changes nothing unless something changed.

  → [What we have: architecture](docs/architecture.md)

## Start here (by role)

| You want to… | Read |
|---|---|
| understand what runs and where | [docs/architecture.md](docs/architecture.md) |
| add a machine to the cluster | [docs/adding-nodes.md](docs/adding-nodes.md): a 5-step checklist, one command |
| run AX tasks and fan-out workflows | [docs/ax-guide.md](docs/ax-guide.md) |
| run Gitea Actions CI on the cluster | [docs/gitea-runner.md](docs/gitea-runner.md): pod + Docker-in-Docker runners, CI waits for AX |
| hand the cluster to another Claude Code session | [docs/ax-handoff.md](docs/ax-handoff.md) (`uv run just ax-handoff` copies it) |
| know why gVisor and not Kata, where a task's files live | [docs/sandboxing.md](docs/sandboxing.md) |
| upgrade Agent Substrate | [docs/upgrading-substrate.md](docs/upgrading-substrate.md): pin, edits, 3-minute stop, egress test |
| tune memory/CPU or understand a setting | [docs/optimizations.md](docs/optimizations.md) |
| keep the cluster from filling up or overloading; add something that grows | [docs/guardrails.md](docs/guardrails.md): layered limits, the 2026-10-05 outage, a checklist |
| see the requirements, design and decisions | [.kiro/specs/k3s-nixos-cluster/](.kiro/specs/k3s-nixos-cluster/) |

## Quick start (workstation)

Needs `uv`, `git`, `go`, `kubectl` and `podman`. No local Nix is needed; Nix checks run in a container.

```bash
uv sync                        # project venv with pyinfra and just
uv run just kube-local         # point kubectl / k9s / ktop / helm at the cluster
kubectl get nodes
uv run just verify             # full cluster check
uv run just --list             # all tasks
```

## Everyday commands

| Command | Does |
|---|---|
| `just verify` | cluster health, tuning, spread and ingress checks |
| `just add-node <ip>` | install NixOS on a fresh VM and join it ([checklist](docs/adding-nodes.md)) |
| `just k3s` | roll out NixOS/k3s config changes, server first, then agents one at a time |
| `just platform [steps]` | (re)install Substrate + AX from pinned versions |
| `just openobserve` | (re)install the telemetry collectors |
| `just gitea-runner` | (re)deploy the Gitea Actions runners |
| `just ax-fanout NAME N IMAGE cmd…` | submit one workflow as N tasks across both nodes |
| `just ax-logs TASK` | a task's output and exit code |
| `just publish ["msg"]` | push the current tree to the public repo as one commit, after a secret scan |
| `just test`, `just nix-check` | unit tests and lint, Nix evaluation of every node config |
| `uv run python scripts/egress-test.py scoped` | one real task per name prefix: allowed hosts reachable, others refused |

Prefix with `uv run` (e.g. `uv run just verify`). `just` comes from the project venv.

## Repository map

| Path | Contents |
|---|---|
| `inventory.py`, `group_data/` | nodes and every tuning knob |
| `deploys/`, `lib/` | pyinfra deploys (install, k3s, verify, …) and their Python helpers |
| `nix/` | the NixOS flake: disk layout, base, tuning and k3s modules |
| `platform/` | Substrate patch and manifests, WorkerPools, egress policy, OpenObserve values, Gitea runners, pinned versions |
| `tools/egress-watcher/`, `tools/ax-reaper/` | small Go services: egress policy for every actor; deleting finished `ousast-engine-*` tasks |
| `scripts/`, `justfile` | entry points |
| `kubeconfig/` | admin and restricted `ax-deployer` kubeconfigs, **gitignored** (written by `just kubeconfig` / `just ax-deployer`) |
| `secrets/` | tokens and keys, **gitignored** |
| `docs/` | the guides linked above |

## Known limits

- single control-plane node;
- AX v0.3.1 has no auth and no native fan-out, and never marks tasks finished;
- task egress: per-prefix host allow-lists (`platform/substrate/egress-routes/scoped.json`); names
  that match no prefix get any public HTTPS host;
- `just footprint` is not built yet.

Details are in [architecture](docs/architecture.md#known-limits-honest-list).

## About this repository

This is the public reference copy of a working setup. It contains no credentials:

- cluster tokens and registry/CI/telemetry credentials live in `secrets/`;
- kubeconfigs live in `kubeconfig/`.

Both folders are gitignored and are created by the `just` recipes. IP addresses, host names and image
owners (`ghcr.io/norandom`) are this cluster's; change them in `inventory.py`, `group_data/all.py` and
`platform/`.

## License

Apache-2.0, see [LICENSE](LICENSE). `platform/substrate/kube-ax.patch` and `kube-ax-edits.py` modify
[Agent Substrate](https://github.com/agent-substrate/substrate) (Apache-2.0).
