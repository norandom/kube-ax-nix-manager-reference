from scripts.gen_fixtures import FIXTURES, build


def test_nix_fixtures_are_current():
    for name, text in build().items():
        path = FIXTURES / name
        assert path.exists() and path.read_text() == text, (
            f"{path} is stale: run `uv run python scripts/gen_fixtures.py`"
        )
