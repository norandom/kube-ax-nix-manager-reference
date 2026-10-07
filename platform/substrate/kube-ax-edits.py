#!/usr/bin/env python3
# ruff: noqa: E501  (edits hold exact upstream YAML/Go text; wrapping would obscure the anchors)
"""Apply kube-ax's adaptations to an Agent Substrate checkout, edit by edit.

    python3 platform/substrate/kube-ax-edits.py <substrate-checkout>

Replaces the old diff (kube-ax.patch) for upstream main: each edit anchors on exact upstream text and
fails loudly if the anchor is gone, so a Substrate bump shows precisely which adaptation needs review.
Running it twice is a no-op (already-applied edits are detected).
"""

from __future__ import annotations

import sys
from pathlib import Path

S3_ENV = """        - name: ATE_STORAGE_BACKEND
          value: "s3"
        # kube-ax: snapshots go to the operator's rustfs S3 API at 10.0.0.54:9000 (no GCS).
        - name: AWS_REGION
          value: us-east-1
        - name: AWS_ENDPOINT_URL
          value: http://10.0.0.54:9000
        - name: AWS_S3_USE_PATH_STYLE
          value: "true"
        - name: AWS_ACCESS_KEY_ID
          valueFrom:
            secretKeyRef: {name: ate-s3-credentials, key: access-key}
        - name: AWS_SECRET_ACCESS_KEY
          valueFrom:
            secretKeyRef: {name: ate-s3-credentials, key: secret-key}
"""
GCS_ENV = """        - name: ATE_STORAGE_BACKEND
          value: "gcs"
"""
GO_CAP = """        # kube-ax: Go soft memory cap + eager GC (measured before: 184-261 MiB per node).
        - name: GOMEMLIMIT
          value: "160MiB"
        - name: GOGC
          value: "50"
"""

# (file, old, new, marker that proves the edit is already applied)
EDITS: list[tuple[str, str, str, str]] = [
    # Snapshots on the operator's rustfs instead of GCS.
    ("manifests/ate-install/ate-api-server.yaml", GCS_ENV, S3_ENV, "AWS_ENDPOINT_URL"),
    ("manifests/ate-install/atelet.yaml", GCS_ENV, S3_ENV + GO_CAP, "AWS_ENDPOINT_URL"),
    # Not on GKE/EKS: no host credential-provider plugins (same as upstream's kind overlay).
    # Actor images are pulled anonymously (public images).
    (
        "manifests/ate-install/atelet.yaml",
        "        - --image-credential-provider-config=/run/image-credential-provider/config.yaml\n"
        "        - --image-credential-provider-bin-dir=/run/image-credential-provider/bin\n",
        "        # kube-ax: no --image-credential-provider-* (GKE host paths); public actor images.\n",
        "kube-ax: no --image-credential-provider",
    ),
    (
        "manifests/ate-install/atelet.yaml",
        "        - name: image-credential-provider-bin\n"
        "          mountPath: /run/image-credential-provider/bin\n"
        "          readOnly: true\n"
        "        - name: image-credential-provider-config\n"
        "          mountPath: /run/image-credential-provider/config.yaml\n"
        "          readOnly: true\n",
        "",
        "",
    ),
    (
        "manifests/ate-install/atelet.yaml",
        "      - name: image-credential-provider-bin\n"
        "        hostPath:\n"
        "          path: /home/kubernetes/bin\n"
        "          type: DirectoryOrCreate\n"
        "      - name: image-credential-provider-config\n"
        "        hostPath:\n"
        "          path: /etc/srv/kubernetes/cri_auth_config.yaml\n"
        "          type: FileOrCreate\n",
        "",
        "",
    ),
    # Cap the actor image cache (2026-10-05: 18 image versions, 14 GiB, filled the node disk).
    (
        "manifests/ate-install/atelet.yaml",
        "        - --drain-delay=0s\n",
        "        - --image-cache-max-bytes=10737418240  # kube-ax: 10 GiB cap\n"
        "        - --image-cache-high-percent=75\n"
        "        - --image-cache-low-percent=65\n"
        "        - --drain-delay=0s\n",
        "image-cache-max-bytes",
    ),
    # Small cluster: one API replica (idle trimming).
    (
        "manifests/ate-install/ate-api-server.yaml",
        "  replicas: 2\n",
        "  replicas: 1  # kube-ax: small cluster, idle trimming (upstream 2)\n",
        "kube-ax: small cluster",
    ),
    # Postgres sized for 8 GiB nodes, on kube-01's storage volume.
    (
        "manifests/ate-install/postgres/postgres.yaml",
        '            cpu: "2"\n            memory: "1Gi"\n          limits:\n            cpu: "16"\n            memory: "2Gi"',
        '            cpu: "250m"\n            memory: "384Mi"\n          limits:\n            cpu: "2"\n            memory: "1Gi"',
        'cpu: "250m"',
    ),
    (
        "manifests/ate-install/postgres/postgres.yaml",
        "          storage: 500Gi",
        "          storage: 8Gi  # kube-ax: on the node's local-path storage LV",
        "storage: 8Gi",
    ),
    (
        "manifests/ate-install/postgres/postgres.yaml",
        "    spec:\n      securityContext:\n",
        "    spec:\n      # kube-ax: pin to the server node; its local-path volume lives there.\n"
        "      nodeSelector:\n        kubernetes.io/hostname: kube-01\n      securityContext:\n",
        "kubernetes.io/hostname: kube-01",
    ),
    # GKE Managed Prometheus object; no such CRD here.
    (
        "manifests/ate-install/base/kustomization.yaml",
        "  - ../atenet-router-monitoring.yaml\n",
        "  # kube-ax: PodMonitoring is GKE Managed Prometheus only.\n",
        "kube-ax: PodMonitoring",
    ),
    # Short "no policy yet" deny cache (watcher attaches policies ~1 s after actor start).
    (
        "manifests/ate-install/atenet-egress.yaml",
        "        - --mode=egress\n",
        "        - --mode=egress\n        - --egress-policy-cache-ttl=2s  # kube-ax (default 10s)\n",
        "egress-policy-cache-ttl=2s",
    ),
    # One actor per worker (operator decision 2026-10-04).
    (
        "cmd/ateom-gvisor/main.go",
        'pflag.Int("max-actors", 1000,',
        'pflag.Int("max-actors", 1, /* kube-ax: one task per worker (upstream 1000) */',
        "kube-ax: one task per worker",
    ),
]


def main() -> int:
    root = Path(sys.argv[1])
    failed = 0
    for rel, old, new, marker in EDITS:
        path = root / rel
        text = path.read_text()
        # Check "already applied" first: some edits keep their anchor (they insert next to it).
        if (marker and marker in text) or (not marker and new == "" and old not in text):
            print(f"present   {rel}: {(marker or 'removed block')[:60]}")
        elif old in text:
            path.write_text(text.replace(old, new, 1))
            print(f"applied   {rel}: {(marker or old.strip().splitlines()[0])[:60]}")
        else:
            print(f"MISSING   {rel}: anchor not found: {old.strip().splitlines()[0][:70]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
