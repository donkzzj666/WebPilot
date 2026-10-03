"""Rebuild from source and locks in a new project directory, sharing download caches only."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "artifacts/verification/M1-25" / f"clean-install-{stamp}"
    target = ROOT / ".cache" / f"clean-install-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    target.mkdir(parents=True, exist_ok=False)
    report = {"task": "M1-25", "passed": False, "clean_checkout": str(target), "commands": [],
              "scope": "New venv and node_modules from locks; shared package/download caches, no copied installation or business data."}
    try:
        for directory in ("backend", "frontend", "scripts", "tests", "config", "requirements"):
            shutil.copytree(ROOT / directory, target / directory,
                            ignore=shutil.ignore_patterns("node_modules", "dist", "__pycache__", ".DS_Store"))
        for name in ("pyproject.toml", ".python-version", ".node-version"):
            shutil.copy2(ROOT / name, target / name)
        (target / "docs/m1").mkdir(parents=True)
        shutil.copy2(ROOT / "docs/m1/dependencies.md", target / "docs/m1/dependencies.md")
        (target / ".cache").mkdir()
        for name in ("pip", "npm", "ms-playwright"):
            (ROOT / ".cache" / name).mkdir(exist_ok=True)
            (target / ".cache" / name).symlink_to(ROOT / ".cache" / name, target_is_directory=True)
        env = {name: value for name, value in os.environ.items() if not name.startswith("WEBAGENT_") and name not in {"PYTHONPATH", "NODE_OPTIONS", "VIRTUAL_ENV"}}
        env.update({"WEBAGENT_PYTHON": str(Path(sys._base_executable).resolve()),
                    "WEBAGENT_NODE": str((ROOT / ".runtime/node").resolve()),
                    "WEBAGENT_NPM_CLI": (ROOT / ".runtime/npm-cli.path").read_text().strip(),
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        for index, command in enumerate((["./scripts/bootstrap.sh"], ["./scripts/check.sh", "--headed"])):
            log = output / f"{index + 1:02d}-command.log"
            with log.open("w") as stream:
                # The complete check includes the M1-24 fixed fault groups.
                # Paid public acceptance remains an explicit separate command.
                result = subprocess.run(command, cwd=target, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=1200 if index == 0 else 1800)
            report["commands"].append({"command": command, "exit_code": result.returncode, "log": log.name})
            if result.returncode:
                raise RuntimeError(f"{command[0]} failed; see {log.name}")
        for directory in (target / "artifacts/verification/M1-25").iterdir():
            shutil.copytree(directory, output / directory.name,
                            ignore=shutil.ignore_patterns('.security', '.private', '.owned'))
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    report["artifact_sha256"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*")) if path.is_file() and not any(part in ('.security', '.private', '.owned') for part in path.parts)
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report": str(output / "report.json"), "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
