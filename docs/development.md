# 数据与开发说明

## 本地数据

每部作品拥有独立目录：

```text
Library/<work-id>/
  book.json             作品信息与卷清单
  volumes/<volume>.json  章节、正文、HTML 与稳定定位信息
  work.sqlite3          Wiki、对话、进度、书签、索引与分析任务
  sources/              导入的 EPUB 与原始资源
  assets/               作品封面
```

正文 JSON 是可编辑来源，SQLite 保存可重建的正文缓存及生成数据。移动作品时，关闭应用并复制完整目录，包括存在的 SQLite WAL 文件；数据库缺失时可以重建正文索引，但无法恢复已生成的 Wiki、对话与阅读状态。移除作品会把完整目录移到书库 `.trash` 下。

设备偏好与模型设置保存在 Electron `userData`。API Key 由 Electron `safeStorage` 加密，独立存为 `.secret` 文件。免安装版仍使用当前用户的配置目录；加密凭据不能直接跨设备搬移。

## 运行与构建

主程序使用 Electron Forge，Windows 发布仅启用 ZIP maker。后端通过 PyInstaller onedir 构建并随主程序分发。`package-lock.json` 和 `uv.lock` 固定依赖版本。

```powershell
npm ci
uv sync --locked
npm run dev
```

```powershell
npm run typecheck
npm run make -- --platform=win32 --arch=x64
```

`UNDERSTORY_DEVICE` 和 `UNDERSTORY_LIBRARY` 可分别指定配置目录与书库目录。

独立只读检查器：

```powershell
npm run build:inspector
```

输出为 `build/tools/UnderStory-Inspector/UnderStory-Inspector.exe`，不包含在主程序 ZIP 中。

## 预发布版本

Electron 使用 SemVer，例如 `0.1.0-alpha.1`；Python 项目使用对应的 `0.1.0a1`。Git tag 使用 `v0.1.0-alpha.1`。新版本应同时更新两个项目清单与锁文件，并重新构建。

发布目录的 `artifacts` 保存应用 ZIP、完整源码 ZIP 与 SHA-256 校验文件。GitHub Release 在审查前保留为草稿，仓库保留为私有；审查通过后再修改可见性并发布预览版。
