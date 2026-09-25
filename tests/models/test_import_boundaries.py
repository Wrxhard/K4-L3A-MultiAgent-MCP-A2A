from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_models_package_imports_in_a_fresh_interpreter() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(root / "src"), existing_path) if path
    )

    result = subprocess.run(
        [sys.executable, "-c", "import student_agent.models"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
