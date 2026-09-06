import json
from pathlib import Path




def _read_package_json(project_root: Path) -> dict:
    package_file = project_root / "package.json"

    if not package_file.is_file():
        return {}

    try:
        return json.loads(package_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _has_nextjs_evidence(project_root: Path) -> bool:
    config_files = (
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
    )

    if any((project_root / filename).is_file() for filename in config_files):
        return True

    package = _read_package_json(project_root)

    dependencies = {}
    dependencies.update(package.get("dependencies", {}))
    dependencies.update(package.get("devDependencies", {}))

    return "next" in dependencies


def _detect_profile(project_root: Path) -> str:
    if _has_nextjs_evidence(project_root):
        return "nextjs"

    if (project_root / "package.json").is_file():
        return "node"

    if any(
        (project_root / filename).is_file()
        for filename in ("pyproject.toml", "requirements.txt", "setup.py")
    ):
        return "python"

    if (project_root / "composer.json").is_file():
        return "php"

    return "generic"


def detect_project(project_root: str | Path) -> str:
    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError(f"Project root does not exist: {root}")

    return _detect_profile(root)
