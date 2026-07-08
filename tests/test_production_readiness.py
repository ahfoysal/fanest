from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_package_declares_typed_wheel_and_build_targets():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (ROOT / "src/fanest/py.typed").exists()
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/fanest"]
    assert "pyright>=1.1.411" in pyproject["project"]["optional-dependencies"]["dev"]
    assert "build>=1.2.0" in pyproject["project"]["optional-dependencies"]["dev"]


def test_ci_runs_release_gate_commands():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run ruff check ." in workflow
    assert "uv run pyright src/fanest" in workflow
    assert "uv run pytest" in workflow
    assert "uv build" in workflow
