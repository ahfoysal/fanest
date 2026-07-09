from pathlib import Path
import importlib.util

import pytest

VERIFY_RELEASE = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
spec = importlib.util.spec_from_file_location("verify_release", VERIFY_RELEASE)
assert spec is not None and spec.loader is not None
verify_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_release)
_assert_distributions = verify_release._assert_distributions
_assert_no_root_scratch_files = verify_release._assert_no_root_scratch_files
_assert_project_metadata = verify_release._assert_project_metadata
_source_version = verify_release._source_version


def test_release_verifier_rejects_extra_dist_files(tmp_path: Path):
    wheel = tmp_path / "fanest-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "fanest-1.2.3.tar.gz"
    extra = tmp_path / ".gitignore"
    wheel.write_text("", encoding="utf-8")
    sdist.write_text("", encoding="utf-8")
    extra.write_text("*\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="Unexpected release files"):
        _assert_distributions(tmp_path, [wheel, sdist], "1.2.3")


def test_release_verifier_accepts_exact_current_artifacts(tmp_path: Path):
    wheel = tmp_path / "fanest-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "fanest-1.2.3.tar.gz"
    wheel.write_text("", encoding="utf-8")
    sdist.write_text("", encoding="utf-8")

    _assert_distributions(tmp_path, [wheel, sdist], "1.2.3")


def test_release_verifier_checks_project_metadata():
    _assert_project_metadata()


def test_source_version_matches_package_metadata():
    assert _source_version() == verify_release._project_version()


def test_release_verifier_rejects_root_scratch_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text("# ok\n", encoding="utf-8")
    (tmp_path / "scratch-release-notes.tmp").write_text("remove me\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="Remove scratch files"):
        _assert_no_root_scratch_files()


def test_release_verifier_accepts_clean_repository_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)
    for filename in [".gitignore", "LICENSE", "README.md", "pyproject.toml", "uv.lock"]:
        (tmp_path / filename).write_text("", encoding="utf-8")

    _assert_no_root_scratch_files()


def test_repository_root_has_no_scratch_files():
    _assert_no_root_scratch_files()


def test_release_smoke_install_exercises_installed_cli_and_generated_project(
    tmp_path: Path,
    monkeypatch,
):
    commands: list[tuple[tuple[str, ...], Path]] = []

    class StaticTemporaryDirectory:
        def __init__(self, prefix: str) -> None:
            self.prefix = prefix

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *args: object) -> None:
            return None

    def fake_run(*command: str, cwd: Path | None = None) -> None:
        commands.append((command, cwd or verify_release.ROOT))

    monkeypatch.setattr(verify_release.tempfile, "TemporaryDirectory", StaticTemporaryDirectory)
    monkeypatch.setattr(verify_release, "_run", fake_run)

    verify_release._smoke_install(tmp_path / "fanest-1.2.3-py3-none-any.whl")

    project = tmp_path / "smoke_api"
    assert ((str(tmp_path / "venv/bin/fanest"), "info"), tmp_path) in commands
    assert ((str(tmp_path / "venv/bin/fanest"), "new", "smoke_api"), tmp_path) in commands
    assert ((str(tmp_path / "venv/bin/fanest"), "check", "main.py"), project) in commands
    assert ((str(tmp_path / "venv/bin/fanest"), "build"), project) in commands
    assert any("TestClient(generated.app)" in " ".join(command) for command, cwd in commands if cwd == project)
