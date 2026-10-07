"""Fetch the server's kubeconfig into kubeconfig/<cluster>.yaml (local only, gitignored).

just kubeconfig      (runs against the server; never pushes)
"""

from pyinfra import host, logger
from pyinfra.facts.server import Command
from pyinfra.operations import python

from lib.kubeconfig import rewrite, save

if host.data.get("role") != "server":
    logger.info("%s: not the server, nothing to fetch", host.name)
else:
    raw = host.get_fact(Command, command="cat /etc/rancher/k3s/k3s.yaml", _sudo=True)
    cluster = host.data.get("cluster_name")
    content = rewrite(raw, host.name, cluster)

    def store() -> None:
        logger.info("kubeconfig/%s.yaml: %s", cluster, save(content, cluster))

    python.call(name=f"Write kubeconfig/{cluster}.yaml", function=store)
