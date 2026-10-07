# Sandboxing: why tasks run in gVisor (and what Kata would be)

**Short answer:** every AX task runs inside **gVisor**, a sandbox that gives the task its own small "fake
kernel". If the task's code is buggy or hostile, it hits gVisor, not the real Linux kernel of our machines.
We prepared the nodes for the stronger **Kata micro-VM** sandbox too, but AX v0.3.1 only supports gVisor,
so that's what runs today.

## 1. The problem a sandbox solves

A normal container is a process with a fence around it, but it still talks **directly to the machine's
real kernel**: every file it opens and every network packet it sends is a request to that one shared kernel.
If the code inside finds a kernel bug, it can escape the fence and reach everything on the machine: other
tasks, credentials, the cluster.

That's an acceptable risk for code you wrote and reviewed. It is not acceptable for **agent workloads**,
where an AI agent generates and runs code nobody reviewed, or downloads and executes things from the
internet. AX and Agent Substrate assume task code is **untrusted** and isolate every task.

## 2. The two sandbox types, in plain words

| | **gVisor** (what we use) | **Kata micro-VM** (prepared, not active) |
|---|---|---|
| Idea | A **stand-in kernel**, written in a memory-safe language, that answers the task's requests and passes only a small, checked set through to the real kernel | A **tiny virtual machine** per task, with its own real kernel, on hardware virtualization |
| Analogy | A receptionist who handles every request and only forwards a few to the back office | A separate small office building per visitor |
| Isolation | Strong: the task never talks to the host kernel directly | Strongest: a hardware boundary (CPU virtualization) |
| Needs | Nothing special | `/dev/kvm` (nested virtualization on Proxmox) |
| Overhead | Small memory overhead; **system calls and I/O are slower**, pure computation runs at normal speed | ~30–60 MiB per VM plus a guest kernel; near-native speed for CPU *and* syscalls |
| Start/suspend | Fast checkpoint/restore | Memory snapshot + on-demand restore |

## 3. Why gVisor here, and why that's fine for our workloads

- **AX v0.3.1 hard-codes gVisor.** It always asks Substrate for the `gvisor-default` sandbox; there is no
  setting to choose Kata.
- **Our heavy work is CPU-bound** (numpy/BLAS, JVM engines). Computation inside gVisor runs at normal CPU
  speed with **AVX2** available, measured: SHA-256 over 1 GiB in about 9 s inside a gVisor task. What gVisor slows down
  is "many small system calls" (lots of tiny file operations, very chatty networking). Batch math and JVM
  engines do relatively few of those.
- **Less to run.** No micro-VM kernels, firmware or per-task VMs to manage and keep patched.

## 4. What we prepared for Kata (micro-VMs) anyway

Both nodes already have what micro-VMs need, so switching later is a software change, not a rebuild:

- Proxmox CPU type `host` with **nested virtualization**, so `/dev/kvm` exists in both VMs;
- the KVM kernel module loaded by NixOS (`kvm-intel`), checked by `just verify`;
- Substrate's node agent already advertises `/dev/kvm` to the scheduler.

Missing for micro-VMs: AX support (or calling Substrate directly), the `ateom-microvm` worker image and
the micro-VM `SandboxConfig`. Revisit when AX supports choosing the sandbox class.

## 5. Where a task's files live (measured 2026-10-06)

Inside the sandbox, a task sees three kinds of storage. We wrote 1 GB to each and watched the worker's
memory and the node's disk:

| Path | Where the data really is | Counts against the worker's 3 GiB as |
|---|---|---|
| `/` (the image's own files) | a file on the node's disk (gVisor's root overlay, `root:self`) | file cache: the kernel can free it |
| `/workspace` | the node's disk (`/var/lib/ate`, on the containerd volume) | file cache: the kernel can free it |
| `/tmp` | **RAM** (a RAM disk inside the sandbox) | shared memory: **stays until the file is deleted** |

**What this means for task authors:** big temporary files don't belong in `/tmp`. Set
`TMPDIR=/workspace/tmp` (create it first) or use `/var/tmp`; Python, the JVM (`-Djava.io.tmpdir`) and
most tools follow it. A 1 GB file in `/tmp` takes a third of the worker's memory.

We had planned a test pool with disk-backed sandboxes. It isn't needed: gVisor already keeps `/` on disk.
Only `/tmp` is RAM, and `TMPDIR` fixes that without a platform change.

## 6. The "golden" copy you may see in the logs

For every new task, Substrate briefly starts **one extra copy** of it (atespace `ate-golden`, named by a
random ID). It saves a snapshot of the freshly started sandbox, so later starts of the same task can
restore quickly, and deletes the copy. It runs for about a second, has no network, and its files are
thrown away.

Two side effects:

- The copy holds a worker for that second. A task submitted in that window can get
  "no free workers available". Clients retry for a minute.
- Your command does start inside the copy. Count runs only from log lines with `ate.atespace = default`.

## 7. What the sandbox does *not* cover

A sandbox stops a task from escaping to the machine. It does **not** stop a task from:

- **Talking to the network.** That's what the egress controls are for: each task gets the host list of
  its name prefix (or none), on HTTPS only, and internal services are blocked at network level
  ([AX guide](ax-guide.md#7a-network-access-from-tasks), [optimizations](optimizations.md#5-safety-rails)).
- **Using too much CPU or memory.** That's handled by the worker's limits and kubelet eviction
  ([optimizations](optimizations.md#2-memory)).
- **Controlling AX.** AX has no authentication. Anyone who can reach `ax-server` controls all tasks. That's
  why its kubeconfig is restricted and treated like a password.
