"""ui/lockdeps.py: only the installed wheel and the wheels uploaded with it are locked (no PyPI traffic)."""
import pytest

from ui import lockdeps


def wheel(filename, uploaded, sha):
    return {"packagetype": "bdist_wheel", "filename": filename, "upload_time_iso_8601": uploaded,
            "digests": {"sha256": sha}}


FILES = [
    {"packagetype": "sdist", "filename": "pkg-1.0.tar.gz", "upload_time_iso_8601": "2026-01-01T10:00:00Z",
     "digests": {"sha256": "sdist"}},
    wheel("pkg-1.0-cp313-cp313-macosx_11_0_arm64.whl", "2026-01-01T10:05:00.123456Z", "mine"),
    wheel("pkg-1.0-cp313-cp313-manylinux_2_17_x86_64.whl", "2026-01-01T11:00:00Z", "linux"),
    wheel("pkg-1.0-cp313-cp313-macosx_14_0_arm64.whl", "2026-09-30T00:00:00Z", "added-later"),
]


def test_locks_the_installed_wheel_and_its_release_batch_only(capsys):
    assert lockdeps.wheel_hashes("pkg", "1.0", tags={"cp313-cp313-macosx_11_0_arm64"}, files=FILES) == ["linux", "mine"]
    assert "macosx_14_0_arm64" in capsys.readouterr().out  # said why it was left out


def test_refuses_when_the_installed_wheel_is_not_on_pypi():
    with pytest.raises(SystemExit, match="not on PyPI"):
        lockdeps.wheel_hashes("pkg", "1.0", tags={"cp313-cp313-win_amd64"}, files=FILES)


def test_reads_the_installed_tags():
    assert any(t.startswith("cp3") or t.startswith("py3") for t in lockdeps.installed_tags("packaging"))
