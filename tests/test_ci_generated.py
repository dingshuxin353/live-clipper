"""Exercise the generated-resource boundary against an actual Git checkout."""

import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parents[1] / "scripts/ci/check-generated.sh"
RESOURCES = (
    "frontend/src/theme/venus-stone-overrides.css",
    "src/live_clipper/web_static/react/index.html",
    "src/live_clipper/web_static/react/assets/app.js",
)


@pytest.mark.parametrize("change", ["clean", "modified", "deleted", "untracked", "ignored", "staged", "unrelated"])
@pytest.mark.parametrize("resource", RESOURCES)
def test_generated_resources(tmp_path, change, resource):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    for name in RESOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    git("add", ".")
    git("-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid", "commit", "-m", "initial")
    path = tmp_path / resource
    if change in {"modified", "staged"}:
        path.write_text("changed\n")
        if change == "staged":
            git("add", resource)
    elif change == "deleted":
        path.unlink()
    elif change in {"untracked", "ignored"}:
        # Recreated untracked output at the exact theme path must also be detected.
        git("rm", resource)
        git("-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid", "commit", "-m", "remove output")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("new output\n")
        if change == "ignored":
            (tmp_path / ".gitignore").write_text(resource + "\n")
    elif change == "unrelated":
        (tmp_path / "unrelated.txt").write_text("not a generated resource\n")
    result = subprocess.run(["bash", str(CHECK)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == (0 if change in {"clean", "unrelated"} else 1), result.stdout + result.stderr
    if result.returncode:
        assert resource in result.stdout
