from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_package_declares_typed_wheel_and_build_targets():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (ROOT / "src/fanest/py.typed").exists()
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/fanest"]
    assert "fastapi>=0.115.0" in pyproject["project"]["dependencies"]
    assert "uvicorn>=0.30.0" in pyproject["project"]["dependencies"]
    assert "uvicorn[standard]>=0.30.0" in pyproject["project"]["optional-dependencies"]["standard"]
    assert "pyright>=1.1.411" in pyproject["project"]["optional-dependencies"]["dev"]
    assert "build>=1.2.0" in pyproject["project"]["optional-dependencies"]["dev"]
    assert "twine>=6.0.0" in pyproject["project"]["optional-dependencies"]["dev"]


def test_ci_runs_release_gate_commands():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run ruff check ." in workflow
    assert "uv run pyright src/fanest" in workflow
    assert "uv run pytest" in workflow
    assert "uv build" in workflow
    assert "Verify release artifacts" in workflow
    assert "uv run python scripts/verify_release.py" in workflow


def test_release_workflow_uses_trusted_publishing_and_distribution_checks():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags:' in workflow
    assert '"v*.*.*"' in workflow
    assert "id-token: write" in workflow
    assert "uv run python scripts/verify_release.py" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow


def test_release_verifier_checks_version_metadata_and_smoke_installs():
    verifier = (ROOT / "scripts/verify_release.py").read_text(encoding="utf-8")

    assert "GITHUB_REF_NAME" in verifier
    assert "does not match pyproject version" in verifier
    assert "twine" in verifier
    assert "fanest/py.typed" in verifier
    assert "_smoke_install(wheel)" in verifier
    assert "_smoke_install(sdist)" in verifier
