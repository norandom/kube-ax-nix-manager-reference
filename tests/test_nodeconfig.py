import json

import pytest

from lib import nodeconfig as nc
from tests.conftest import network_for


def render(host, nodes, cpu, phase="k3s", **overrides):
    addr, data = host
    return nc.render(addr, data | overrides, network_for(addr), cpu, nodes, phase)


def test_server_shape(server, agent, cpu):
    cfg = render(server, [server, agent], cpu)
    assert cfg["schema"] == 1 and cfg["phase"] == "k3s"
    assert cfg["kvm"] == "intel"
    assert cfg["network"]["address"] == "10.0.0.63/24"
    assert cfg["diskLayout"] == {"root": "20G", "containerd": "35G", "storage": "30G"}
    k3s = cfg["k3s"]
    assert k3s["role"] == "server" and k3s["entryPoint"] is True and k3s["serverAddr"] is None
    assert k3s["flannelBackend"] == "host-gw"
    assert k3s["labels"][nc.ENTRYPOINT_LABEL] == "true"
    assert k3s["kubelet"]["kubeReserved"]["memory"] == "512Mi"
    assert k3s["kubelet"]["swapBehavior"] == "NoSwap"
    assert k3s["kubelet"]["kernelMemcgNotification"] is True


def test_agent_shape(server, agent, cpu):
    k3s = render(agent, [server, agent], cpu)["k3s"]
    assert k3s["role"] == "agent"
    assert k3s["serverAddr"] == "https://10.0.0.63:6443"
    assert k3s["entryPoint"] is False
    assert nc.ENTRYPOINT_LABEL not in k3s["labels"]
    assert k3s["flannelBackend"] == "host-gw" and "disable" not in k3s
    assert k3s["kubelet"]["kubeReserved"]["memory"] == "256Mi"


@pytest.mark.parametrize("key", ["node_name", "disk", "boot_mode"])
def test_missing_inventory_field(server, cpu, key):
    addr, data = server
    data = {k: v for k, v in data.items() if k != key}
    with pytest.raises(nc.NodeConfigError, match=key):
        nc.render(addr, data, network_for(addr), cpu, [(addr, data)], "base")


def test_missing_network_field(server, cpu):
    addr, data = server
    net = network_for(addr) | {"gateway": None}
    with pytest.raises(nc.NodeConfigError, match="gateway"):
        nc.render(addr, data, net, cpu, [server], "base")


def test_address_must_match_capture(server, cpu):
    addr, data = server
    with pytest.raises(nc.NodeConfigError, match="captured address"):
        nc.render(addr, data, network_for("10.0.0.99"), cpu, [server], "base")


def test_exactly_one_server(server, cpu):
    second = ("10.0.0.70", server[1] | {"node_name": "kube-09"})
    with pytest.raises(nc.NodeConfigError, match="exactly one server"):
        render(server, [server, second], cpu)
    agent_only = ("10.0.0.64", server[1] | {"role": "agent"})
    with pytest.raises(nc.NodeConfigError, match="exactly one server"):
        render(agent_only, [agent_only], cpu)


@pytest.mark.parametrize(
    "flags,match",
    [
        ({"avx2": False}, "avx2"),
        ({"fma": False}, "fma"),
        ({"kvm": None}, "nested"),
        ({"dev_kvm": False}, "nested"),
    ],
)
def test_cpu_requirements(server, cpu, flags, match):
    with pytest.raises(nc.NodeConfigError, match=match):
        render(server, [server], cpu | flags)


def test_flannel_auto():
    assert nc.flannel_backend("auto", "10.0.0.63/24", ["10.0.0.63", "10.0.0.64"]) == "host-gw"
    assert nc.flannel_backend("auto", "10.0.0.63/24", ["10.0.0.63", "10.0.1.5"]) == "vxlan"
    assert nc.flannel_backend("vxlan", "10.0.0.63/24", ["10.0.0.63"]) == "vxlan"


def test_flannel_auto_with_remote_agent(server, cpu):
    remote = ("10.0.1.5", server[1] | {"role": "agent", "node_name": "kube-03"})
    assert render(server, [server, remote], cpu)["k3s"]["flannelBackend"] == "vxlan"
    # The agent derives the same cluster-wide backend.
    assert render(remote, [server, remote], cpu)["k3s"]["flannelBackend"] == "vxlan"


def test_static_cpu_manager_needs_reserved_cpus(server, cpu):
    with pytest.raises(nc.NodeConfigError, match="reserved_cpus"):
        render(server, [server], cpu, cpu_manager_policy="static")
    cfg = render(server, [server], cpu, cpu_manager_policy="static", reserved_cpus="0")
    assert cfg["k3s"]["kubelet"]["reservedCpus"] == "0"


@pytest.mark.parametrize(
    "key,value",
    [
        ("swap_mode", "disk"),
        ("swap_behavior", "UnlimitedSwap"),
        ("cpu_mitigations", "maybe"),
        ("boot_mode", "efi"),
        ("role", "worker"),
    ],
)
def test_invalid_enums(server, cpu, key, value):
    with pytest.raises(nc.NodeConfigError, match=key if key != "role" else "role"):
        render(server, [(server[0], server[1] | {key: value})], cpu, **{key: value})


@pytest.mark.parametrize(
    "layout,match",
    [
        ({"root": "20G", "containerd": "35G"}, "storage"),
        ({"root": "20G", "containerd": "35G", "storage": "30G", "swap": "4G"}, "unknown"),
        ({"root": "twenty", "containerd": "35G", "storage": "30G"}, "root"),
    ],
)
def test_disk_layout_validation(server, cpu, layout, match):
    with pytest.raises(nc.NodeConfigError, match=match):
        render(server, [server], cpu, disk_layout=layout)


def test_tuning_and_sysctl_overrides(server, cpu):
    cfg = render(server, [server], cpu, sysctl_overrides={"vm.swappiness": 60}, swap_mode="zswap")
    assert cfg["tuning"]["swapMode"] == "zswap"
    assert cfg["tuning"]["sysctl"] == {"vm.swappiness": 60}


def test_empty_ssh_keys_rejected(server, cpu):
    with pytest.raises(nc.NodeConfigError, match="admin_ssh_keys"):
        render(server, [server], cpu, admin_ssh_keys=[])


def test_dumps_is_stable(server, cpu):
    a = nc.dumps(render(server, [server], cpu))
    b = nc.dumps(json.loads(a))
    assert a == b and a.endswith("\n")
