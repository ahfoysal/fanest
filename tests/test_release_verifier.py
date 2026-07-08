from pathlib import Path
import importlib.util

import pytest

VERIFY_RELEASE = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
spec = importlib.util.spec_from_file_location("verify_release", VERIFY_RELEASE)
assert spec is not None and spec.loader is not None
verify_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_release)
_assert_distributions = verify_release._assert_distributions


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
