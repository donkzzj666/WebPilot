"""Check report hashes after each verification process has exited."""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    results = []
    for path in args.reports:
        report = json.loads(path.read_text())
        directory = path.resolve().parent
        errors = []
        for name, expected in report["artifact_sha256"].items():
            artifact = (directory / name).resolve()
            if not artifact.is_relative_to(directory) or not artifact.is_file():
                errors.append({"file": name, "reason": "missing or outside evidence directory"})
            elif hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                errors.append({"file": name, "reason": "hash mismatch"})
        results.append({"report": str(path), "passed": report["passed"] and not errors,
                        "checked_files": len(report["artifact_sha256"]), "errors": errors})
    passed = all(result["passed"] for result in results)
    print(json.dumps({"passed": passed, "reports": results}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
