"""A release tag must agree with every version in the repo (hack/check-release-version.sh), and the release
workflow must check that before it builds anything."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "hack" / "check-release-version.sh"
FILES = ("pyproject.toml", "src/booth_pipeline/__init__.py", "charts/booth-pipeline/Chart.yaml")


def check(tag: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    if shutil.which("sh") is None:
        pytest.skip("no sh here; CI has it")
    return subprocess.run(["sh", SCRIPT.as_posix(), tag, root.as_posix()], capture_output=True, text=True, check=False)


def current() -> str:
    return re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)


def tree(tmp_path: Path) -> Path:
    for f in FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / f, tmp_path / f)
    return tmp_path


def test_the_repo_agrees_with_itself_with_or_without_the_tags_v():
    assert check(f"v{current()}").returncode == 0
    assert check(current()).returncode == 0


def test_a_tag_the_repo_does_not_say_fails_naming_all_of_them():
    out = check("v99.0.0")
    assert out.returncode == 1
    for name in ("project version", "__version__", "chart version", "chart appVersion"):
        assert name in out.stderr


@pytest.mark.parametrize(
    ("file", "old", "new", "named"),
    [
        ("pyproject.toml", 'version = "{v}"', 'version = "0.0.1"', "project version"),
        ("src/booth_pipeline/__init__.py", '__version__ = "{v}"', '__version__ = "0.0.1"', "__version__"),
        ("charts/booth-pipeline/Chart.yaml", "version: {v}\n", "version: 0.0.1\n", "chart version"),
        ("charts/booth-pipeline/Chart.yaml", 'appVersion: "{v}"', 'appVersion: "0.0.1"', "chart appVersion"),
    ],
)
def test_one_file_left_behind_fails(tmp_path, file, old, new, named):
    root = tree(tmp_path)
    path = root / file
    text = path.read_text(encoding="utf-8")
    needle = old.format(v=current())
    assert needle in text
    path.write_text(text.replace(needle, new, 1), encoding="utf-8", newline="\n")
    out = check(f"v{current()}", root)
    assert out.returncode == 1 and named in out.stderr


def test_the_release_workflow_checks_before_it_builds_or_publishes():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    check_at = workflow.index("hack/check-release-version.sh")
    assert check_at < workflow.index("docker/login-action") < workflow.index("docker/build-push-action")
    assert workflow.index("needs: image") > check_at  # the chart job only runs after the image job, so after the check
