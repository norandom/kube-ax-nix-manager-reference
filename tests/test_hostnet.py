import pytest

from lib import hostnet
from tests.conftest import network_for


def test_merge_overrides_take_precedence():
    captured = network_for("10.0.0.63")
    net = hostnet.merge(captured, {"gateway": "10.0.0.254", "dns": "1.1.1.1", "ip_prefix": 16})
    assert net["gateway"] == "10.0.0.254"
    assert net["dns"] == ["1.1.1.1"]
    assert net["prefix"] == 16
    assert net["mac"] == captured["mac"]


def test_merge_without_overrides_keeps_capture():
    captured = network_for("10.0.0.63")
    assert hostnet.merge(captured, {}) == captured


def test_save_is_idempotent_and_guards_changes(tmp_path):
    net = network_for("10.0.0.63")
    assert hostnet.save("kube-01", net, root=tmp_path) is True
    assert hostnet.save("kube-01", net, root=tmp_path) is False
    changed = net | {"gateway": "10.0.0.254"}
    with pytest.raises(hostnet.NetworkChangedError, match="gateway"):
        hostnet.save("kube-01", changed, root=tmp_path)
    assert hostnet.save("kube-01", changed, recapture=True, root=tmp_path) is True
    assert hostnet.load("kube-01", root=tmp_path)["gateway"] == "10.0.0.254"
