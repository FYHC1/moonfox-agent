"""Unified checks. Run with the interpreter containing the development dependencies."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installation", action="store_true", help="also build/install isolated wheels"
    )
    parser.add_argument(
        "--work-dir", type=Path, help="parent directory for temporary check artifacts"
    )
    parser.add_argument(
        "--web", action="store_true", help="also test and build the independent WebUI"
    )
    args = parser.parse_args()
    if args.work_dir is not None:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mfx-", dir=args.work_dir) as directory:
        work = Path(directory)
        env = os.environ.copy()
        env["MOONFOX_RUN_INSTALLATION"] = "1" if args.installation else "0"
        for name in ("TMPDIR", "TEMP", "TMP"):
            env[name] = str(work)
        commands = [
            ["ruff", "check", ".", "--no-cache"],
            ["ruff", "format", ".", "--check", "--no-cache"],
            ["mypy", "--cache-dir", str(work / "mypy")],
            [
                "pytest",
                "-q",
                "--basetemp",
                str(work / "pytest"),
                "-o",
                f"cache_dir={work / 'cache'}",
            ],
        ]
        for command in commands:
            print(f"==> {sys.executable} -m {' '.join(command)}", flush=True)
            result = subprocess.run(
                [sys.executable, "-m", *command], cwd=ROOT, env=env, check=False
            )
            if result.returncode:
                return result.returncode
        result = subprocess.run(
            [sys.executable, "scripts/export_protocol.py", "--check"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if result.returncode:
            return result.returncode
        if args.web:
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
            if npm is None:
                print("Node/npm is required for --web; backend-only checks do not need it")
                return 1
            for task in ("test", "build"):
                result = subprocess.run(
                    [npm, "run", task], cwd=ROOT / "packages/web-ui", env=env, check=False
                )
                if result.returncode:
                    return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
