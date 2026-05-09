# GitHub 小白检索器 · github-newbie-finder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg)
![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B.svg)

🌐 **语言**：中文 · [English](README.en.md)

> 用一句中文描述你想做的事，工具帮你找 GitHub 项目、判断难度、写一份新手能跟着跑的部署教程。
>
> _Describe your need in one sentence — get the top GitHub projects ranked for beginners, plus a personalised, step-by-step deployment tutorial. (Bilingual zh/en export to Markdown for Obsidian.)_

> 关于 UI 语言：本工具主要面向中文用户，**界面文字保持中文**。LLM 产出的内容（推荐摘要、优缺点、教程、Markdown 导出）会**跟随侧边栏「教程语言」**（zh / en）。

---

## 界面预览

输入需求 → 自动召回 + 评分 → 看推荐 → 一键生成教程，全程在网页里完成。

**1. 输入需求 · 自适应 Top-N**

候选越多 / 高 star 越多，自动放宽到 Top-7 或 Top-10。

![search](docs/screenshots/1-search.png)

**2. Top 10 对比表**

5 个维度（需求 / 新手 / 部署 / 活跃 / 文档）一览，挑得明白。

![comparison](docs/screenshots/2-recommendations-table.png)

**3. 推荐卡片：评分明细 + LLM 分析 + Evidence**

每条推荐都标着分值依据，并附 LLM 的优缺点分析与 README 证据片段。

![cards](docs/screenshots/3-recommendations-cards.png)

**4. 部署教程：按你的 OS / 水平定制**

不确定的步骤会被打上「⚠️ 需要核对」，避免 LLM 一本正经地胡说命令。

![tutorial](docs/screenshots/4-tutorial.png)

---

## 它能做什么

1. **解析需求**：把一句"想找一个本地能跑的 RAG 知识库，最好有 Web UI"翻译成结构化条件 + 英文搜索关键词
2. **召回 + 评分**：调 GitHub Search 召回候选 → 抓 README/tree → 5 维规则打分（需求匹配 / 新手友好 / 部署难度 / 活跃度 / 文档）→ LLM 重排
3. **写部署教程**：基于 repo 快照 + 你的画像（OS / 水平 / 教程语言），生成可执行的 step-by-step 教程；不确定的步骤会被打上 `needs_verification` 标记
4. **沉淀**：⭐ 收藏、🗂 历史回看、⬇️ 一键导出 Markdown 到 Obsidian

## 主要特性

- 🔑 **多 API Key 在网页里管理**：加 / 切 / 删 deepseek / openai / anthropic 凭证，无需改 `.env` 重启服务
- 🤖 **可插拔 LLM provider**：deepseek / openai / anthropic，或 `LLM_PROVIDER=echo` 演示模式（无 key 也能跑通整个流程）
- 📊 **解释性强**：每个推荐都附 5 维评分明细 + LLM pros/cons + README 证据片段
- ⭐ **历史可回放**：所有查询、推荐、教程都进 SQLite，历史页一键回到当时的结果
- ⬇️ **Markdown 导出（中英双语）**：推荐 / 教程导出成带 YAML frontmatter 的 `.md`，丢进 Obsidian vault 就能用
- 🎯 **推荐数自适应**：高 star（≥500）候选多时，自动从 Top-5 放宽到 Top-7 / Top-10
- 🔁 **LLM 调用全部可重放**：每次 LLM I/O 写进 `llm_calls` 表，调试不抓瞎

## 快速开始

```bash
git clone https://github.com/<your-username>/github-newbie-finder.git
cd github-newbie-finder

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # 想用真 LLM 就编辑这个；保持 echo 也能演示

# Terminal A — 后端（推荐：日志会写到 logs/backend.log）
python -m app.cli init-db
bash scripts/run-backend.sh        # 后台运行
# 或：bash scripts/run-backend.sh fg  # 前台运行，看实时日志

# Terminal B — 前端
streamlit run streamlit_app.py
```

> 也可以直接 `uvicorn app.main:app --reload --port 8000`，但出问题时 traceback 会丢；用脚本启动则始终落盘到 `logs/backend.log`，遇到 500 时 `tail -f logs/backend.log` 直接看堆栈。

打开 streamlit 给的 URL（默认 `http://localhost:8501`），首页就是「需求检索」。

### 之后日常启动（双击即可）

#### macOS

初次安装完成后，建两个桌面双击入口：

```bash
chmod +x scripts/launch.command scripts/stop.command
ln -sf "$PWD/scripts/launch.command" ~/Desktop/"启动 GitHub 小白检索器.command"
ln -sf "$PWD/scripts/stop.command"   ~/Desktop/"停止 GitHub 小白检索器.command"
```

- 双击 **`启动 GitHub 小白检索器.command`** → 启动 backend（`:8000`）和 Streamlit（`:8501`），已在跑则跳过，不会重复杀进程，等就绪后自动打开 `http://localhost:8501`。
- 双击 **`停止 GitHub 小白检索器.command`** → 停止上述后台服务。

> ⚠️ 服务通过 `nohup` 在后台运行：**关闭浏览器或 Terminal 窗口都不会停止它们**，必须用上面的"停止"双击或手动 `pkill -f 'uvicorn app.main' ; pkill -f 'streamlit run streamlit_app'`。

#### Windows

不需要 `chmod` 或 `ln`。Windows 的入口直接放在 `scripts\` 下：

- 双击 **`scripts\launch.bat`** → 启动 backend (`:8000`) + Streamlit (`:8501`) + 自动打开 `http://localhost:8501`。两个最小化的 cmd 窗口会出现在任务栏，里面是后端 / 前端的实时日志（关掉它们等于停止服务）。
- 双击 **`scripts\stop.bat`** → 停止 backend 和 Streamlit。

想要桌面快捷方式：右键 `launch.bat` → "发送到" → "桌面 (创建快捷方式)"，把生成的 `.lnk` 重命名即可（`stop.bat` 同理）。

> ⚠️ Windows 版同样把服务放在后台（独立的最小化 cmd 窗口）：**关闭浏览器不会停止它们**。停止请双击 `stop.bat`，不要直接 `taskkill /F /IM python.exe`（会误杀其它 Python 进程）。
>
> 前置：必须先按上面"快速开始"在项目根目录建好 `.venv` 并 `pip install -r requirements.txt`。Windows 用户的初次安装命令是：
>
> ```cmd
> python -m venv .venv
> .venv\Scripts\activate
> pip install -r requirements.txt
> copy .env.example .env
> python -m app.cli init-db
> ```

### 配置 API Key 的两种方式

1. **网页里管理（推荐）**：侧边栏「🔑 API Key 管理」展开，填表保存，激活按钮切换。多套配置存在 SQLite 里，跨重启保留。
2. **`.env` 老办法**：照 `.env.example` 改，重启 uvicorn。当网页里没激活任何 Key 时回退到这套。

> 推荐 [DeepSeek](https://platform.deepseek.com)：OpenAI 兼容 API，便宜，跑这个项目一杯奶茶能用很久。

### 想直接试用，没 API Key？

把 `.env` 里的 `LLM_PROVIDER` 改成 `echo`，整个流程会用确定性 stub 跑（仍然有真实的 GitHub 召回和规则打分，只是 LLM 部分换成"模仿器"）。够你看清这个工具是怎么工作的。

## 项目结构

```
app/
  schemas.py         # 所有 Pydantic 模型（LLM 接口契约都在这）
  db.py              # SQLite 持久化层（SQLAlchemy core）
  llm.py             # LLM provider 抽象 + 调用录音
  parser.py          # 自然语言 → RequirementParseResult
  query_planner.py   # → list of GitHubQuery
  github.py          # GitHub REST 客户端（recall + snapshot）
  profiler.py        # repo 快照 → 安装信号（Docker / pkg / GPU…）
  scorer.py          # 5 维规则打分 + S1-S5 难度分级
  ranker.py          # LLM 重排，融合规则分
  tutorial.py        # 用户画像 + repo 快照 → TutorialPlan
  exporters.py       # 推荐 / 教程 → Markdown（zh / en）
  main.py            # FastAPI 应用
  cli.py             # python -m app.cli init-db / replay <id>
streamlit_app.py     # 5 页 UI
data/finder.db       # SQLite（git 忽略，文件权限自动 0600）
```

## 演示场景

- 想找一个适合新手本地部署的 RAG 知识库，最好有 Web UI，不想折腾 Docker
- 开源的发票 / 小票 OCR，能用来整理报销
- 不用 Docker、Windows 上跑得起来的图片压缩 / 管理工具

## 工作流程一览

```
你的中文需求
  ↓ LLM #1 解析
解析结果（must_have / nice_to_have / avoid / keywords_en …）
  ↓ query_planner
5-8 条 GitHub 搜索 query
  ↓ github.recall
候选仓库 list
  ↓ github.fetch_snapshot
README + tree + topics + license
  ↓ profiler
InstallSignals（has_compose / needs_gpu / pkg managers …）
  ↓ scorer
5 维规则分 + 难度分级 + 硬过滤
  ↓ LLM #2 重排
RepoAnalysisResult（fit/beginner/pros/cons/evidence）
  ↓ ranker.merge
Top-N 推荐（含解释 + evidence）
  ↓ 用户点「📘 生成部署教程」
LLM #3 教程
  ↓ TutorialPlan + 你的画像
个性化部署教程
  ↓ 用户点「⬇️ 导出 Markdown」
Obsidian-friendly .md
```

## 路线图 / Ideas

- [ ] 支持把推荐 / 教程直接写到 Obsidian vault 路径（现在只下载 .md）
- [ ] 引入 GitHub Topics 召回路径，提升召回多样性
- [ ] 提供 docker-compose 一键启动 backend+frontend
- [ ] 实验：beginner_score 走一次小模型微调

欢迎提 Issue / PR。

## 安全 & 注意事项

- 工具**不会自动执行**任何部署命令。教程里 `needs_verification=true` 的步骤说明 LLM 拿不准，请你自己核对后再跑
- GitHub 搜索有 rate limit；填 `GITHUB_TOKEN` 后从 10 → 30 req/min
- API Key 在 SQLite 里**明文**存储，`finder.db` 文件已自动 `chmod 0600`。如果你的设备多人共用，请用 `.env` 而不是网页保存
- 第一次启动会调 GitHub Search + 三次 LLM，如果 LLM 不稳定可临时切到 `LLM_PROVIDER=echo` 走演示

## License

MIT — see [LICENSE](LICENSE).
