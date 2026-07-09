from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    version = _project_version()
    _assert_project_metadata(version)
    tag = os.environ.get("GITHUB_REF_NAME")
    if tag and tag.startswith("v") and tag[1:] != version:
        raise SystemExit(f"Release tag {tag!r} does not match pyproject version {version!r}.")

    dist_dir = ROOT / "dist"
    shutil.rmtree(dist_dir, ignore_errors=True)
    _run("uv", "build")
    (dist_dir / ".gitignore").unlink(missing_ok=True)
    distributions = [*sorted(dist_dir.glob("*.whl")), *sorted(dist_dir.glob("*.tar.gz"))]
    _assert_distributions(dist_dir, distributions, version)
    _run("uv", "run", "twine", "check", *map(str, distributions))
    wheel = next(dist_dir.glob("fanest-*.whl"))
    sdist = next(dist_dir.glob("fanest-*.tar.gz"))
    _assert_wheel_metadata(wheel)
    _smoke_install(wheel)
    _smoke_install(sdist)
    print(f"release verification ok: {version}")


def _project_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def _project_metadata() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _source_version() -> str:
    init_file = ROOT / "src" / "fanest" / "__init__.py"
    for line in init_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("src/fanest/__init__.py is missing __version__.")


def _assert_project_metadata(version: str | None = None) -> None:
    project = _project_metadata()
    expected_version = version or project["version"]
    source_version = _source_version()
    if source_version != expected_version:
        raise SystemExit(
            f"Package version mismatch: pyproject has {expected_version!r}, "
            f"fanest.__version__ has {source_version!r}."
        )
    scripts = project.get("scripts", {})
    if scripts.get("fanest") != "fanest.cli.main:app":
        raise SystemExit("pyproject.toml must expose the fanest CLI script.")
    dependencies = set(project.get("dependencies", []))
    for dependency in {"fastapi>=0.115.0", "typer>=0.12.0", "uvicorn>=0.30.0"}:
        if dependency not in dependencies:
            raise SystemExit(f"Missing required runtime dependency: {dependency}")
    optional = project.get("optional-dependencies", {})
    if "uvicorn[standard]>=0.30.0" not in optional.get("standard", []):
        raise SystemExit("The standard extra must install uvicorn[standard].")
    if not (ROOT / "src" / "fanest" / "py.typed").exists():
        raise SystemExit("src/fanest/py.typed is missing.")


def _assert_distributions(dist_dir: Path, distributions: list[Path], version: str) -> None:
    expected = {
        f"fanest-{version}-py3-none-any.whl",
        f"fanest-{version}.tar.gz",
    }
    actual = {path.name for path in dist_dir.iterdir()}
    if actual != expected:
        raise SystemExit(f"Unexpected release files in dist: {sorted(actual)!r}; expected {sorted(expected)!r}.")
    if {path.name for path in distributions} != expected:
        raise SystemExit("Release distributions did not include exactly the current wheel and sdist.")


def _assert_wheel_metadata(wheel: Path) -> None:
    with ZipFile(wheel) as zf:
        names = set(zf.namelist())
        if "fanest/py.typed" not in names:
            raise SystemExit("Wheel is missing fanest/py.typed.")
        if not any(name.endswith(".dist-info/entry_points.txt") for name in names):
            raise SystemExit("Wheel is missing console entry point metadata.")


def _smoke_install(distribution: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="fanest-release-smoke-") as tmp:
        venv = Path(tmp) / "venv"
        _run("uv", "venv", str(venv))
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        _run("uv", "pip", "install", "--python", str(python), str(distribution))
        _run(
            str(python),
            "-c",
            (
                "from fanest import Controller, FaNestFactory, Get, Module\n"
                "from fanest.cli.main import app as cli\n"
                "@Controller('smoke')\n"
                "class C:\n"
                "    @Get('/')\n"
                "    async def index(self): return {'ok': True}\n"
                "@Module(controllers=[C])\n"
                "class M: pass\n"
                "app = FaNestFactory.create(M); "
                "assert callable(app); assert callable(cli)"
            ),
        )


def _run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
