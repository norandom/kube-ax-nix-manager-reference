import stat

from lib import secrets


def test_token_generated_once_with_private_mode(tmp_path):
    first = secrets.k3s_token(tmp_path)
    assert len(first) == 64
    assert secrets.k3s_token(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "k3s-token").stat().st_mode) == 0o600


def test_optional_secret(tmp_path):
    assert secrets.optional("ghcr-token", tmp_path) is None
    (tmp_path / "ghcr-token").write_text("ghp_x\n")
    assert secrets.optional("ghcr-token", tmp_path) == "ghp_x"
