# M1-01 依赖版本与许可清单

生成时间（UTC）：2026-09-29T04:16:04.774521+00:00。由 `scripts/build_manifest.py` 离线读取已安装元数据与锁文件生成。

仅在依赖升级并完成验证后显式更新；日常启动不重写。使用 `.venv/bin/python scripts/build_manifest.py --check` 检查漂移。

完整字段、原许可路径和 SHA-256 见 [build-manifest.json](../../config/build-manifest.json)；浏览器版本、修订与二进制摘要见 [browser-lock.json](../../config/browser-lock.json)。

## 运行时与锁定范围

| 项目 | 已核实版本 / 范围 |
| --- | --- |
| Python | 3.12.14 (CPython) |
| Python 实际链接 SQLite | 3.53.1 |
| 前端 Node | 24.19.0；来自 `.node-version` 与 `.runtime/node --version` |
| npm | 10.8.2；核对 frontend/package.json 的 packageManager |
| Playwright 内部 Node driver | 24.21.0；Playwright wheel 自带，与前端 Node 独立 |
| Python 已安装依赖 | 59 项，包含运行与开发工具 |
| npm 锁定依赖 | 70 项，包含本平台未安装的 optional 包 |

| 锁定输入 | SHA-256 |
| --- | --- |
| `requirements.lock` | `63cdf1b2bfa9a8c946ebe48de2f88dea4e205dbbaabac17eee33067f3df9b300` |
| `requirements-dev.lock` | `ba1ffc965a5c3b4a9e2ac142143b960ea249ba72728d9552029b6825878bfe47` |
| `frontend/package-lock.json` | `200655fbde9f2b4a3b0027a6d32e6a2b6d53556c0e74e15b0b4773c128bc95fb` |
| `.python-version` | `f50159fad3f4319868eb38717b91d55843c41e9803014c8de05e116a6d0bcfdc` |
| `.node-version` | `7e8a2fa94951112b894a3dbe3d05efef5e9263741fa49125f0a70f40fedab4cc` |

## Python 依赖

许可标签保持包 METADATA 的原始声明，不擅自改写为 SPDX。元数据只提供分类器时，明确显示分类器；wheel 未附许可原文时仍保留元数据来源。

| 包 | 版本 | 声明许可 | 锁文件 |
| --- | --- | --- | --- |
| aiosqlite | 0.22.1 | License :: OSI Approved :: MIT License | requirements.lock, requirements-dev.lock |
| annotated-doc | 0.0.5 | MIT | requirements.lock, requirements-dev.lock |
| annotated-types | 0.8.0 | MIT | requirements.lock, requirements-dev.lock |
| anyio | 4.15.1 | MIT | requirements.lock, requirements-dev.lock |
| build | 1.6.1 | MIT | requirements-dev.lock |
| certifi | 2026.7.22 | MPL-2.0 | requirements.lock, requirements-dev.lock |
| charset-normalizer | 3.5.1 | MIT | requirements.lock, requirements-dev.lock |
| click | 8.5.0 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| distro | 1.9.0 | Apache License, Version 2.0 | requirements.lock, requirements-dev.lock |
| fastapi | 0.141.1 | MIT | requirements.lock, requirements-dev.lock |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | requirements.lock, requirements-dev.lock |
| h11 | 0.16.0 | MIT | requirements.lock, requirements-dev.lock |
| httpcore | 1.0.9 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| httpcore2 | 2.13.1 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| httpx | 0.28.1 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| httpx2 | 2.13.1 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| idna | 3.20 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| iniconfig | 2.3.0 | MIT | requirements-dev.lock |
| jsonpatch | 1.33 | Modified BSD License | requirements.lock, requirements-dev.lock |
| jsonpointer | 3.1.1 | Modified BSD License | requirements.lock, requirements-dev.lock |
| langchain-core | 1.6.5 | MIT | requirements.lock, requirements-dev.lock |
| langchain-protocol | 0.0.19 | MIT | requirements.lock, requirements-dev.lock |
| langgraph | 1.2.12 | MIT | requirements.lock, requirements-dev.lock |
| langgraph-checkpoint | 4.2.0 | MIT | requirements.lock, requirements-dev.lock |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT | requirements.lock, requirements-dev.lock |
| langgraph-prebuilt | 1.1.0 | MIT | requirements.lock, requirements-dev.lock |
| langgraph-sdk | 0.4.5 | MIT | requirements.lock, requirements-dev.lock |
| langsmith | 0.14.1 | MIT | requirements.lock, requirements-dev.lock |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | requirements.lock, requirements-dev.lock |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT | requirements.lock, requirements-dev.lock |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | requirements.lock, requirements-dev.lock |
| pip | 25.0.1 | MIT | requirements-dev.lock |
| pip-tools | 7.6.1 | BSD | requirements-dev.lock |
| playwright | 1.63.0 | Apache-2.0 | requirements.lock, requirements-dev.lock |
| pluggy | 1.6.0 | MIT | requirements-dev.lock |
| pydantic | 2.13.5 | MIT | requirements.lock, requirements-dev.lock |
| pydantic_core | 2.46.5 | MIT | requirements.lock, requirements-dev.lock |
| pyee | 13.0.1 | MIT | requirements.lock, requirements-dev.lock |
| Pygments | 2.21.0 | BSD-2-Clause | requirements-dev.lock |
| pyproject_hooks | 1.3.3 | MIT | requirements-dev.lock |
| pytest | 9.1.1 | MIT | requirements-dev.lock |
| PyYAML | 6.0.3 | MIT | requirements.lock, requirements-dev.lock |
| requests | 2.34.2 | Apache-2.0 | requirements.lock, requirements-dev.lock |
| requests-toolbelt | 1.0.0 | Apache 2.0 | requirements.lock, requirements-dev.lock |
| setuptools | 84.0.0 | MIT | requirements-dev.lock |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | requirements.lock, requirements-dev.lock |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 | requirements.lock, requirements-dev.lock |
| starlette | 1.7.0 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| tenacity | 9.1.4 | Apache 2.0 | requirements.lock, requirements-dev.lock |
| truststore | 0.10.4 | MIT | requirements.lock, requirements-dev.lock |
| typing_extensions | 4.16.0 | PSF-2.0 | requirements.lock, requirements-dev.lock |
| typing-inspection | 0.4.4 | MIT | requirements.lock, requirements-dev.lock |
| urllib3 | 2.8.0 | MIT | requirements.lock, requirements-dev.lock |
| uuid_utils | 0.17.1 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| uvicorn | 0.54.0 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| websockets | 16.1.1 | BSD-3-Clause | requirements.lock, requirements-dev.lock |
| wheel | 0.48.0 | MIT | requirements-dev.lock |
| xxhash | 4.0.1 | BSD-2-Clause | requirements.lock, requirements-dev.lock |
| zstandard | 0.25.0 | BSD-3-Clause | requirements.lock, requirements-dev.lock |

## npm 依赖

以下标签来自 `frontend/package-lock.json` 的 `license` 字段；完整 integrity、平台限制与 optional 标记保存在构建清单。

| 包 | 版本 | 声明许可 | 范围 |
| --- | --- | --- | --- |
| @oxc-project/types | 0.151.0 | MIT | 开发 |
| @rolldown/binding-android-arm-eabi | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-android-arm64 | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-darwin-arm64 | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-darwin-x64 | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-freebsd-x64 | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-arm-gnueabihf | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-arm64-gnu | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-arm64-musl | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-ppc64-gnu | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-s390x-gnu | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-x64-gnu | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-linux-x64-musl | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-openharmony-arm64 | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-win32-arm64-msvc | 1.2.11 | MIT | 开发 / optional |
| @rolldown/binding-win32-x64-msvc | 1.2.11 | MIT | 开发 / optional |
| @rolldown/pluginutils | 1.0.1 | MIT | 开发 |
| @types/node | 24.19.0 | MIT | 开发 |
| @types/react | 19.3.0 | MIT | 开发 |
| @types/react-dom | 19.3.0 | MIT | 开发 |
| @typescript/typescript-aix-ppc64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-darwin-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-darwin-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-freebsd-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-freebsd-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-arm | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-loong64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-mips64el | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-ppc64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-riscv64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-s390x | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-linux-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-netbsd-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-netbsd-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-openbsd-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-openbsd-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-sunos-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-win32-arm64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @typescript/typescript-win32-x64 | 7.0.2 | Apache-2.0 | 开发 / optional |
| @vitejs/plugin-react | 6.1.1 | MIT | 开发 |
| csstype | 3.2.3 | MIT | 开发 |
| detect-libc | 2.1.2 | Apache-2.0 | 开发 |
| fdir | 6.5.0 | MIT | 开发 |
| fsevents | 2.3.3 | MIT | 开发 / optional |
| lightningcss | 1.33.0 | MPL-2.0 | 开发 |
| lightningcss-android-arm64 | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-darwin-arm64 | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-darwin-x64 | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-freebsd-x64 | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-linux-arm-gnueabihf | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-linux-arm64-gnu | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-linux-arm64-musl | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-linux-x64-gnu | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-linux-x64-musl | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-win32-arm64-msvc | 1.33.0 | MPL-2.0 | 开发 / optional |
| lightningcss-win32-x64-msvc | 1.33.0 | MPL-2.0 | 开发 / optional |
| nanoid | 3.3.19 | MIT | 开发 |
| picocolors | 1.1.1 | ISC | 开发 |
| picomatch | 4.0.7 | MIT | 开发 |
| postcss | 8.5.28 | MIT | 开发 |
| react | 19.3.0 | MIT | 运行 |
| react-dom | 19.3.0 | MIT | 运行 |
| rolldown | 1.2.11 | MIT | 开发 |
| scheduler | 0.28.0 | MIT | 运行 |
| source-map-js | 1.2.1 | BSD-3-Clause | 开发 |
| tinyglobby | 0.2.17 | MIT | 开发 |
| typescript | 7.0.2 | Apache-2.0 | 开发 |
| undici-types | 7.24.6 | MIT | 开发 |
| vite | 8.3.1 | MIT | 开发 |

## 浏览器与附带许可资源

| 组件 | 版本 | Playwright revision | 可执行文件 SHA-256 |
| --- | --- | --- | --- |
| chromium | 153.0.8010.12 | 1243 | `8319963f6625accf51c0dd4f55091ceaf9f09ed39e7a52fed4fae12b2a6b668a` |
| chromium-headless-shell | 153.0.8010.12 | 1243 | `a0bfe7b4da4787b66058477d696cd1d09065d25f06a548947722b9af77ee8282` |

Chromium 发行物含多项第三方组件。Chrome for Testing 的 `ABOUT` 将开源声明指向 `chrome://credits`，条款指向 `chrome://terms`；没有将整个发行物归为单一许可。

可离线定位的资源（文件摘要见 browser-lock.json）：

- `.cache/ms-playwright/chromium-1243/chrome-mac-arm64/ABOUT`
- `.cache/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/Frameworks/Google Chrome for Testing Framework.framework/Versions/153.0.8010.12/Libraries/WidevineCdm/LICENSE`
- `.cache/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/Frameworks/Google Chrome for Testing Framework.framework/Versions/153.0.8010.12/Resources/resources.pak`
- `.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/ABOUT`
- `.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/LICENSE.headless_shell`
- `.cache/ms-playwright/ffmpeg-1011/COPYING.LGPLv2.1`（ffmpeg revision 1011）

Playwright driver 附带的 Node 及第三方声明：

- `.venv/lib/python3.12/site-packages/playwright/driver/LICENSE`
- `.venv/lib/python3.12/site-packages/playwright/driver/package/LICENSE`
- `.venv/lib/python3.12/site-packages/playwright/driver/package/NOTICE`
- `.venv/lib/python3.12/site-packages/playwright/driver/package/ThirdPartyNotices.txt`

## 核查边界

此清单记录依赖身份和许可来源，不表示这些包的全部功能均被产品启用。LangSmith 是 LangGraph 的传递依赖；运行入口在框架导入前关闭外部 tracing，实际出站验证另见 M1-01 集成证据。

依赖和 Chromium 的锁定不能代替 M1-12 网络防护，也不代表 FR-01 的 M1-16 产品图与新进程恢复验收完成。
