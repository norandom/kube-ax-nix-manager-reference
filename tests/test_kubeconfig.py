import subprocess

from lib.kubeconfig import rewrite, save

K3S = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: AAA
    server: https://127.0.0.1:6443
  name: default
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
kind: Config
users:
- name: default
  user:
    client-certificate-data: BBB
"""


def test_rewrite_server_and_names():
    out = rewrite(K3S, "10.0.0.63", "kube-ax")
    assert "server: https://10.0.0.63:6443" in out
    assert "default" not in out
    assert out.count("kube-ax") == 6
    assert "client-certificate-data: BBB" in out


def _repo(tmp_path):
    for cmd in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True)
    return tmp_path


def test_save_writes_private_file_only_when_changed(tmp_path):
    assert save("x\n", "c", tmp_path) == "written"
    path = tmp_path / "kubeconfig" / "c.yaml"
    assert path.read_text() == "x\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert save("x\n", "c", tmp_path) == "unchanged"
    assert save("y\n", "c", tmp_path) == "written"
