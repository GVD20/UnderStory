<div align="center">

<img src="assets/brand/app-icon.png" width="96" alt="UnderStory 标志：从书中生长的树">

# UnderStory

**一款 AI 原生的轻小说阅读器**

栽一棵智能的树，为阅读而生长。

![版本](https://img.shields.io/badge/版本-0.1.0--alpha.1-347645)
![平台](https://img.shields.io/badge/平台-Windows_x64-586777)
[![MIT](https://img.shields.io/badge/许可证-MIT-347645)](LICENSE)

[下载预览版](https://github.com/GVD20/UnderStory/releases) · [开始使用](#开始使用) · [从源码运行](#从源码运行)

</div>

## 阅读之外，故事之内

UnderStory 围绕你导入的 EPUB，提供**阅读、Wiki 与对话**三个相互衔接的空间。作品分析会逐卷阅读原文，梳理人物、事件与世界观，再整理成可以自由浏览的 Wiki 与章节轻览，让辅助资料自然地出现在阅读过程中。

忘记一个名字，可以选中文字查词条；想探索作品，可以沿着人物、组织与专题继续浏览；遇到具体疑问，阅读伙伴会结合 Wiki、跨卷记忆与原文引用回答。故事始终在中间，资料就在手边。

| 功能 | 你可以做什么 |
| :--- | :--- |
| **作品分析** | 逐卷整理作品，积累跨卷记忆，生成 Wiki 与章节概览；可配置语言模型与文本嵌入服务。 |
| **Wiki** | 浏览人物、事件、设定与专题，沿着相关词条探索，通过叙事轨迹和引用回到原文。 |
| **反剧透** | 按阅读进度解锁遮罩，控制词条可见性，并在对话涉及严重剧透时决定是否继续。 |
| **对话** | 围绕当前章节或选中段落提问，也可以用自然语言调整阅读设置并撤回修改。 |
| **划线查询** | 从正文中的名字与别名进入词条，或把选中的文字附给阅读伙伴。 |

阅读器支持分卷导航、字体与行距调整、浅色与深色主题、EPUB 排版和沉浸式插画，可独立用于日常阅读。

*Understory* 指森林树冠下方的植被层。标志中的小树从摊开的书页生长：原文是根，Wiki 与阅读辅助是逐渐展开的枝叶。

## 开始使用

1. 从 [Releases](https://github.com/GVD20/UnderStory/releases) 下载 `windows-x64-portable.zip`，完整解压后运行 `UnderStory.exe`。无需安装 Python 或 Node.js。
2. 导入 EPUB，开始阅读。同一作品可以按卷组织。
3. 如需 Wiki 与对话，在设置中填写模型服务地址、模型名称和 API Key，再从“分析作品”启动质量分析或快速分析。

模型服务使用 OpenAI 风格的 Chat Completions 接口，分析与对话依赖工具调用能力；是否可用取决于服务与模型。语义检索可另行配置 Embedding 服务，未配置时仍可使用词法检索。AI 请求按所配置服务计费。

当前提供 **Windows x64 Alpha 免安装版**。配置保存在当前用户的应用数据目录，书库默认位于 Documents 下的 `UnderStory Library`，可在设置中更改。书库与生成数据保存在本地；调用模型时，会向你配置的服务发送相关文本。

## 从源码运行

需要 Windows、Node.js ≥ 22.13、Python 3.13、[uv](https://docs.astral.sh/uv/) 和 PowerShell 7。

```powershell
git clone https://github.com/GVD20/UnderStory.git
cd UnderStory
npm ci
uv sync --locked
npm run dev
```

构建 Windows x64 免安装 ZIP：

```powershell
npm run typecheck
npm run make -- --platform=win32 --arch=x64
```

输出位于 `out/make/zip/win32/x64/`，可运行目录位于 `out/UnderStory-win32-x64/`。独立数据检查器可通过 `npm run build:inspector` 构建。

## 项目结构

```text
electron/   桌面窗口、本地服务生命周期与设备配置
renderer/   React 阅读器、Wiki、对话和设置界面
backend/    Python EPUB 解析、作品分析、检索与存储
assets/     品牌标志与应用图标
scripts/    开发、构建与发布检查脚本
tools/      独立的只读数据检查器
docs/       数据格式、开发说明与第三方许可证
```

Electron + React + TypeScript + Python/FastAPI + SQLite。分析流程以逐卷 Agent 和公共要点表积累跨卷记忆，再规划、撰写 Wiki；对话可以结合结构化知识与原文检索查证。每部作品独立保存正文 JSON、数据库与资源，详见 [数据与开发说明](docs/development.md)。

## 许可证

UnderStory 源码采用 [MIT](LICENSE) 协议。第三方依赖与资源保留各自的许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
