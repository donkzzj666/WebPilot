#!/usr/bin/env python3
"""Explicitly record the installed build and offline dependency licence evidence.

Run with the project .venv after dependency locking and integration verification.
--check is read-only: it rejects drift without regenerating the saved manifests.
This reads only lockfiles, package metadata and installed runtime/browser files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
BUILD_FILE = ROOT / "config/build-manifest.json"
BROWSER_FILE = ROOT / "config/browser-lock.json"
DEPENDENCIES_FILE = ROOT / "docs/m1/dependencies.md"
LOCKS = ("requirements/requirements.lock", "requirements/requirements-dev.lock", "frontend/package-lock.json")
VERSION_FILES = (".python-version", ".node-version")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(path: Path) -> str:
    # Keep this machine's checkout path out of the distributable manifest.
    return os.path.relpath(path, ROOT)


def file_evidence(path: Path) -> dict:
    return {"path": relative(path), "sha256": sha256(path)}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def command_version(path: Path) -> str:
    environment = os.environ.copy()
    # Manifest inspection must not load unrelated caller-supplied Node hooks.
    environment.pop("NODE_OPTIONS", None)
    result = subprocess.run([str(path), "--version"], check=True, text=True,
                            capture_output=True, timeout=10, env=environment)
    return result.stdout.strip().removeprefix("v")


def python_lock_pins() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in LOCKS[:2]:
        text = (ROOT / name).read_text()
        pins = {canonical(package): version for package, version in
                re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", text, re.MULTILINE)}
        if not pins:
            raise ValueError(f"{name} contains no exact dependency versions")
        result[name] = pins
    return result


def installed_python(pins: dict[str, dict[str, str]]) -> list[dict]:
    packages = []
    for distribution in sorted(metadata.distributions(), key=lambda item: canonical(item.metadata["Name"])):
        meta = distribution.metadata
        name = meta["Name"]
        expression = meta.get("License-Expression")
        legacy = meta.get("License")
        classifiers = [value for value in meta.get_all("Classifier", []) if value.startswith("License ::")]
        licence = expression or legacy or "; ".join(classifiers) or "NOT_DECLARED"
        source = "License-Expression" if expression else "License" if legacy else "Classifier" if classifiers else "missing"
        metadata_file = None
        licence_files = []
        for entry in distribution.files or []:
            path = Path(distribution.locate_file(entry))
            if entry.name == "METADATA" and ".dist-info" in str(entry):
                metadata_file = file_evidence(path)
            if re.match(r"(?i)^(licen[cs]e|notice|copying|thirdpartynotices)([._-].*)?$", entry.name) and path.is_file():
                licence_files.append(file_evidence(path))
        packages.append({
            "name": name, "version": distribution.version,
            "license": licence, "license_source": f"package METADATA / {source}",
            "license_expression": expression, "license_metadata": legacy,
            "license_classifiers": classifiers,
            "metadata_file": metadata_file,
            "license_files": sorted(licence_files, key=lambda item: item["path"]),
            "locked_in": [lock for lock, versions in pins.items() if canonical(name) in versions],
        })
    installed = {canonical(item["name"]): item["version"] for item in packages}
    for lock, versions in pins.items():
        for name, version in versions.items():
            if installed.get(name) != version:
                raise ValueError(f"{lock}: {name} requires {version}, installed {installed.get(name, 'MISSING')}")
    return packages


def npm_packages() -> list[dict]:
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    entries = []
    for path, package in sorted(lock["packages"].items()):
        if not path:
            continue
        entries.append({
            "name": package.get("name", path.rsplit("node_modules/", 1)[-1]),
            "version": package.get("version"), "package_path": path,
            "license": package.get("license", "NOT_DECLARED"),
            "license_source": f"frontend/package-lock.json / packages / {path} / license",
            "integrity": package.get("integrity"), "resolved": package.get("resolved"),
            "dev": package.get("dev", False), "optional": package.get("optional", False),
            "os": package.get("os", []), "cpu": package.get("cpu", []),
        })
    return entries


def executable_in(folder: Path, candidates: tuple[str, ...]) -> Path:
    for pattern in candidates:
        matches = sorted(path for path in folder.glob(pattern) if path.is_file())
        if matches:
            return matches[0]
    raise FileNotFoundError(f"Browser executable missing in {relative(folder)}")


def browser_inventory() -> tuple[dict, dict]:
    distribution = metadata.distribution("playwright")
    driver = Path(distribution.locate_file("playwright/driver"))
    manifest_path = driver / "package/browsers.json"
    manifest = json.loads(manifest_path.read_text())
    browsers = {item["name"]: item for item in manifest["browsers"]}
    chromium = browsers["chromium"]
    headless = browsers["chromium-headless-shell"]
    if chromium["revision"] != headless["revision"] or chromium["browserVersion"] != headless["browserVersion"]:
        raise ValueError("Chromium and headless shell have different expected revisions or versions")
    cache = ROOT / ".cache/ms-playwright"
    items = []
    candidates = {
        "chromium": ("chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing", "chrome-*/Chromium.app/Contents/MacOS/Chromium", "chrome-*/chrome", "chrome-*/chrome.exe"),
        "chromium-headless-shell": ("chrome-headless-shell-*/chrome-headless-shell", "chrome-headless-shell-*/chrome-headless-shell.exe", "chrome-*/headless_shell"),
    }
    for name in ("chromium", "chromium-headless-shell"):
        expected = browsers[name]
        folder = cache / f"{name.replace('-', '_')}-{expected['revision']}"
        executable = executable_in(folder, candidates[name])
        complete = folder / "INSTALLATION_COMPLETE"
        if not complete.is_file():
            raise FileNotFoundError(f"Playwright installation incomplete: {relative(folder)}")
        licence_resources = []
        for path in sorted(folder.rglob("*")):
            if path.is_file() and (re.match(r"(?i)^(licen[cs]e|notice|copying|about)([._-].*)?$", path.name) or path.name == "resources.pak"):
                licence_resources.append(file_evidence(path))
        item = {
            "name": name, "revision": expected["revision"], "version": expected["browserVersion"],
            "version_source": relative(manifest_path), "executable": file_evidence(executable),
            "installation_complete": relative(complete), "license_resources": licence_resources,
        }
        if platform.system() == "Darwin" and name == "chromium":
            plist_path = executable.parents[1] / "Info.plist"
            with plist_path.open("rb") as stream:
                actual_version = plistlib.load(stream)["CFBundleShortVersionString"]
            if actual_version != item["version"]:
                raise ValueError(f"Installed Chromium {actual_version} differs from Playwright manifest {item['version']}")
            item["installed_bundle_version"] = actual_version
            item["bundle_version_source"] = file_evidence(plist_path)
        items.append(item)
    ffmpeg = browsers.get("ffmpeg")
    bundled_support = []
    if ffmpeg:
        folder = cache / f"ffmpeg-{ffmpeg['revision']}"
        if folder.is_dir():
            bundled_support.append({"name": "ffmpeg", "revision": ffmpeg["revision"], "license_files": [file_evidence(path) for path in sorted(folder.glob("COPYING*")) if path.is_file()]})
    driver_node = driver / ("node.exe" if platform.system() == "Windows" else "node")
    driver_info = {
        "version": command_version(driver_node), "executable": file_evidence(driver_node),
        "role": "Playwright bundled Node driver; distinct from the frontend Node runtime",
        "license_resources": [file_evidence(path) for path in (driver / "LICENSE", driver / "package/LICENSE", driver / "package/NOTICE", driver / "package/ThirdPartyNotices.txt") if path.is_file()],
    }
    browser_lock = {
        "schema_version": 1, "platform": platform.system(), "architecture": platform.machine(),
        "playwright_version": distribution.version, "playwright_browser_manifest": file_evidence(manifest_path),
        "browsers": items, "bundled_support": bundled_support,
        "license_note": "Chrome for Testing ABOUT points to chrome://credits for Chromium and third-party software, and chrome://terms for its terms. The installed resources.pak and headless LICENSE.headless_shell are retained as local notice resources; this is an inventory, not a claim that every browser component uses one licence.",
    }
    return browser_lock, driver_info


def collect() -> tuple[dict, dict]:
    required = [ROOT / name for name in LOCKS + VERSION_FILES]
    missing = [relative(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Generate/install the dependency locks first; missing: " + ", ".join(missing))
    pins = python_lock_pins()
    packages = installed_python(pins)
    npm = npm_packages()
    browsers, driver_node = browser_inventory()
    python_expected = (ROOT / ".python-version").read_text().strip()
    frontend_node_expected = (ROOT / ".node-version").read_text().strip()
    frontend_node_path = ROOT / ".runtime/node"
    frontend_node = command_version(frontend_node_path)
    npm_cli = Path((ROOT / ".runtime/npm-cli.path").read_text().strip())
    npm_env = os.environ.copy()
    npm_env.pop("NODE_OPTIONS", None)
    npm_version = subprocess.run([str(frontend_node_path), str(npm_cli), "--version"],
                                 capture_output=True, text=True, check=True, timeout=10,
                                 env=npm_env).stdout.strip()
    npm_expected = json.loads((ROOT / "frontend/package.json").read_text())["packageManager"].split("@", 1)[1]
    if npm_version != npm_expected:
        raise ValueError(f"npm {npm_version} differs from frontend packageManager {npm_expected}")
    if platform.python_version() != python_expected:
        raise ValueError(f"Python {platform.python_version()} differs from .python-version {python_expected}")
    if frontend_node != frontend_node_expected:
        raise ValueError(f"Frontend Node {frontend_node} differs from .node-version {frontend_node_expected}")
    result = {
        "schema_version": 1,
        "platform": {"system": platform.system(), "architecture": platform.machine()},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(), "sqlite_linked_version": sqlite3.sqlite_version},
        "frontend_node": {"version": frontend_node, "executable": file_evidence(frontend_node_path), "role": "React/TypeScript frontend build and Vite development server"},
        "npm": {"version": npm_version, "expected_from": "frontend/package.json packageManager"},
        "playwright_driver_node": driver_node,
        "source_files": [file_evidence(path) for path in required],
        "python_packages": packages, "npm_packages": npm,
        "package_counts": {"python_installed": len(packages), "npm_locked_including_optional": len(npm)},
        "unlocked_installed_python_packages": [item["name"] for item in packages if not item["locked_in"]],
        "license_metadata_missing": {"python": [item["name"] for item in packages if item["license"] == "NOT_DECLARED"], "npm": [item["name"] for item in npm if item["license"] == "NOT_DECLARED"]},
        "license_scope": "Licence labels are copied from installed Python METADATA and npm package-lock metadata; original licence/notice paths and hashes are recorded when present. Optional npm packages remain listed even when not installed on this platform. No licence is inferred from a package name.",
        "browser_lock": "config/browser-lock.json",
    }
    return result, browsers


def table_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def dependency_document(build: dict, browsers: dict, generated_at: str) -> str:
    lines = [
        "# M1-01 依赖版本与许可清单", "",
        f"生成时间（UTC）：{generated_at}。由 `scripts/dependencies/build_manifest.py` 离线读取已安装元数据与锁文件生成。", "",
        "仅在依赖升级并完成验证后显式更新；日常启动不重写。使用 `.venv/bin/python scripts/dependencies/build_manifest.py --check` 检查漂移。", "",
        "完整字段、原许可路径和 SHA-256 见 [build-manifest.json](../../config/build-manifest.json)；浏览器版本、修订与二进制摘要见 [browser-lock.json](../../config/browser-lock.json)。", "",
        "## 运行时与锁定范围", "",
        "| 项目 | 已核实版本 / 范围 |", "| --- | --- |",
        f"| Python | {build['python']['version']} ({build['python']['implementation']}) |",
        f"| Python 实际链接 SQLite | {build['python']['sqlite_linked_version']} |",
        f"| 前端 Node | {build['frontend_node']['version']}；来自 `.node-version` 与 `.runtime/node --version` |",
        f"| npm | {build['npm']['version']}；核对 frontend/package.json 的 packageManager |",
        f"| Playwright 内部 Node driver | {build['playwright_driver_node']['version']}；Playwright wheel 自带，与前端 Node 独立 |",
        f"| Python 已安装依赖 | {len(build['python_packages'])} 项，包含运行与开发工具 |",
        f"| npm 锁定依赖 | {len(build['npm_packages'])} 项，包含本平台未安装的 optional 包 |", "",
        "| 锁定输入 | SHA-256 |", "| --- | --- |",
    ]
    lines.extend(f"| `{item['path']}` | `{item['sha256']}` |" for item in build["source_files"])
    lines += ["", "## Python 依赖", "", "许可标签保持包 METADATA 的原始声明，不擅自改写为 SPDX。元数据只提供分类器时，明确显示分类器；wheel 未附许可原文时仍保留元数据来源。", "",
              "| 包 | 版本 | 声明许可 | 锁文件 |", "| --- | --- | --- | --- |"]
    for item in build["python_packages"]:
        licence = item["license"]
        if len(licence) > 180:
            licence = "长许可正文，见 build-manifest.json 对应 license 字段与原文件"
        lines.append(f"| {table_cell(item['name'])} | {item['version']} | {table_cell(licence)} | {', '.join(item['locked_in']) or '未被依赖锁包含'} |")
    lines += ["", "## npm 依赖", "", "以下标签来自 `frontend/package-lock.json` 的 `license` 字段；完整 integrity、平台限制与 optional 标记保存在构建清单。", "",
              "| 包 | 版本 | 声明许可 | 范围 |", "| --- | --- | --- | --- |"]
    for item in build["npm_packages"]:
        scope = ("开发" if item["dev"] else "运行") + (" / optional" if item["optional"] else "")
        lines.append(f"| {table_cell(item['name'])} | {item['version']} | {table_cell(item['license'])} | {scope} |")
    lines += ["", "## 浏览器与附带许可资源", "", "| 组件 | 版本 | Playwright revision | 可执行文件 SHA-256 |", "| --- | --- | --- | --- |"]
    for item in browsers["browsers"]:
        lines.append(f"| {item['name']} | {item['version']} | {item['revision']} | `{item['executable']['sha256']}` |")
    lines += ["", "Chromium 发行物含多项第三方组件。Chrome for Testing 的 `ABOUT` 将开源声明指向 `chrome://credits`，条款指向 `chrome://terms`；没有将整个发行物归为单一许可。", "",
              "可离线定位的资源（文件摘要见 browser-lock.json）：", ""]
    for item in browsers["browsers"]:
        for resource in item["license_resources"]:
            lines.append(f"- `{resource['path']}`")
    for item in browsers["bundled_support"]:
        for resource in item["license_files"]:
            lines.append(f"- `{resource['path']}`（{item['name']} revision {item['revision']}）")
    lines += ["", "Playwright driver 附带的 Node 及第三方声明：", ""]
    lines.extend(f"- `{item['path']}`" for item in build["playwright_driver_node"]["license_resources"])
    lines += ["", "## 核查边界", "",
              "此清单记录依赖身份和许可来源，不表示这些包的全部功能均被产品启用。LangSmith 是 LangGraph 的传递依赖；运行入口在框架导入前关闭外部 tracing，实际出站验证另见 M1-01 集成证据。", "",
              "依赖和 Chromium 的锁定不能代替 M1-12 网络防护，也不代表 FR-01 的 M1-16 产品图与新进程恢复验收完成。", ""]
    if build["unlocked_installed_python_packages"]:
        lines += ["未出现在两个依赖锁中的已安装 Python 包：" + ", ".join(build["unlocked_installed_python_packages"]) + "。", ""]
    return "\n".join(lines)


def without_timestamp(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "generated_at"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check saved inventory against this installation without modifying any file")
    args = parser.parse_args()
    try:
        build, browsers = collect()
        if args.check:
            previous_build = json.loads(BUILD_FILE.read_text())
            previous_browsers = json.loads(BROWSER_FILE.read_text())
            mismatches = []
            if without_timestamp(previous_build) != build:
                for key in build:
                    if previous_build.get(key) != build[key]:
                        mismatches.append(f"build-manifest.{key}")
            if without_timestamp(previous_browsers) != browsers:
                for key in browsers:
                    if previous_browsers.get(key) != browsers[key]:
                        mismatches.append(f"browser-lock.{key}")
            expected_document = dependency_document(build, browsers, previous_build["generated_at"])
            if DEPENDENCIES_FILE.read_text() != expected_document:
                mismatches.append("docs/m1/dependencies.md")
            if mismatches:
                raise ValueError("Build drift detected: " + ", ".join(mismatches) + ". Review and reverify before explicitly regenerating manifests.")
            print(json.dumps({"passed": True, "mode": "check", "package_counts": build["package_counts"]}))
        else:
            generated_at = datetime.now(timezone.utc).isoformat()
            BUILD_FILE.parent.mkdir(parents=True, exist_ok=True)
            DEPENDENCIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            BUILD_FILE.write_text(json.dumps(build | {"generated_at": generated_at}, ensure_ascii=False, indent=2) + "\n")
            BROWSER_FILE.write_text(json.dumps(browsers | {"generated_at": generated_at}, ensure_ascii=False, indent=2) + "\n")
            DEPENDENCIES_FILE.write_text(dependency_document(build, browsers, generated_at))
            print(json.dumps({"passed": True, "mode": "generate", "files": [relative(BUILD_FILE), relative(BROWSER_FILE), relative(DEPENDENCIES_FILE)], "package_counts": build["package_counts"]}))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
