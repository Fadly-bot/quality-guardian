import json
from pathlib import Path

import pytest

from detection.detector import detect_project


def create_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for filename, content in files.items():
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    return tmp_path


def test_nextjs_detected_by_config(tmp_path):
    project = create_project(
        tmp_path,
        {
            "package.json": "{}",
            "next.config.js": "module.exports = {};",
        },
    )

    assert detect_project(project) == "nextjs"


def test_nextjs_detected_by_dependency(tmp_path):
    package = {
        "dependencies": {
            "next": "^16.0.0"
        }
    }

    project = create_project(
        tmp_path,
        {
            "package.json": json.dumps(package),
        },
    )

    assert detect_project(project) == "nextjs"


def test_node_detected_without_nextjs(tmp_path):
    project = create_project(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "dependencies": {
                        "express": "^5.0.0"
                    }
                }
            ),
        },
    )

    assert detect_project(project) == "node"


def test_python_detected(tmp_path):
    project = create_project(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'example'\n",
        },
    )

    assert detect_project(project) == "python"


def test_php_detected(tmp_path):
    project = create_project(
        tmp_path,
        {
            "composer.json": "{}",
        },
    )

    assert detect_project(project) == "php"


def test_unknown_project_uses_generic(tmp_path):
    project = create_project(
        tmp_path,
        {
            "README.md": "# Example\n",
        },
    )

    assert detect_project(project) == "generic"


def test_invalid_project_root_raises_error(tmp_path):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValueError):
        detect_project(missing)
