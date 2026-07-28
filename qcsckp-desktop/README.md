# 千川素材看盘工具（qianchuan-promotion-crawl）

基于 **Python + pywebview** 的桌面端应用，配合 **Playwright** 在浏览器中访问[巨量千川](https://qianchuan.jinritemai.com)，拦截素材列表接口数据，落库 **SQLite**，并在本地 Web 界面中展示消耗、排行、素材历史曲线等「看盘」能力。支持可选同步**飞书多维表**与**群机器人 Webhook 整点推送**。

> **说明**：浏览器可执行文件探测逻辑（`utils/common.py`）当前以 **Windows + Edge/Chrome 标准安装路径**为主；生产使用建议以 Windows 环境为准。macOS 下 GUI 虽可运行，若遇浏览器路径问题需自行扩展或调整。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 数据采集 | 有头浏览器登录千川 → 进入投放详情页（识别 `aavid` / `adId`）→ 按间隔轮询拦截 `uni-promotion/material/list-required` 接口并写入 SQLite |
| 本地看盘 | 表格、Top 消耗、素材历史折线等（`static/` + `api/dashboard.py`） |
| 账号校验 | 启动采集前经远程服务校验账号（`config.API_BASE_URL`，见 `api/account_auth.py`） |
| 飞书多维表 | 配置 `app_token` / `personal_base_token` / `table_id`，在抓取入库后同步（见 `services/feishu_bitable/`） |
| 飞书 Webhook | 可配置整点推送大屏表格摘要（`services/feishu_webhook_push.py`） |
| 单实例 | 同机重复启动时激活已有窗口并退出新进程（`data/command.json`） |
| 系统托盘 | 关闭窗口可最小化到托盘（依赖 `pystray` + `Pillow`） |
| 数据维护 | 后台线程按行数上限裁剪 SQLite，控制库体积（见下方环境变量） |

---

## 技术栈

- **Python** ≥ 3.12（见 `.python-version`）
- **依赖管理**：[`uv`](https://github.com/astral-sh/uv)（`pyproject.toml` + `uv.lock`）
- **GUI**：`pywebview`
- **自动化**：`playwright`（使用本机 Chromium 内核浏览器：**Microsoft Edge 或 Google Chrome**）
- **数据**：`SQLite`（`utils/sqlite_store.py`）
- **可选**：飞书 Open API（`baseopensdk`）、Windows 更新流程（`services/update_service_win.py`）

---

## 环境要求

1. **操作系统**：推荐 **Windows 10/11**（已安装 **Edge 或 Chrome** 于常见路径）。
2. **Python**：3.12+。
3. **网络**：首次启动采集、账号登录、版本检查等需能访问配置的远程 API（`API_BASE_URL`）及千川站点。

---

## 安装

```bash
# 克隆后进入项目目录
cd qcsckp

# 使用 uv 同步依赖（会创建 .venv）
uv sync
```

若尚未安装 `uv`，可参考官方文档安装后再执行上述命令。

---

## 运行

```bash
# 建议在项目根目录、已激活 uv 虚拟环境
uv run python gui_app.py
```

启动后主窗口加载 `static/index.html`。请在界面内按提示完成**远程账号登录**，再**启动采集服务**。

### 采集流程简述

1. 启动服务后，会打开**有头**浏览器窗口，请在千川完成登录。
2. 进入**投放详情页**，使地址符合配置中的前缀（默认 `https://qianchuan.jinritemai.com/uni-prom/deta...`），并包含 `aavid` 与 `adId`。
3. 程序识别目标后会**保存 Cookie** 到 `data/qcookie.json`（路径可在服务配置中调整），随后可按设定间隔**有头或无头**轮询抓取。
4. 数据写入 `data/qianchuan.db`（默认路径见 `config.DB_FILE`），日志在 `logs/` 下滚动。

---

## 配置说明

### 远程 API 基址

`config.py` 中：

```python
API_BASE_URL = "https://qcscjk.shanghaijiyue.com"
```

账号校验、版本检测等 HTTP 接口均在此基址下拼接路径（详见 `api/account_auth.py` 内注释及仓库内 API 文档，若存在 `dev_files/`）。

### SQLite 自动裁剪（可选）

通过环境变量控制（默认值见 `config.py`）：

| 变量 | 含义 |
|------|------|
| `SQLITE_PRUNE_ENABLED` | 是否启用裁剪（默认 `true`） |
| `SQLITE_PRUNE_MAX_ROWS` | 按主键保留的最大行数（默认 `2000000`） |
| `SQLITE_PRUNE_INTERVAL_SEC` | 裁剪周期（秒，默认 `3600`） |
| `SQLITE_PRUNE_START_DELAY_SEC` | 启动后延迟（秒，默认 `5`） |

### 运行时目录（默认）

| 路径 | 用途 |
|------|------|
| `data/` | Cookie、SQLite、服务配置、飞书 Webhook 配置等 |
| `logs/` | 应用日志（含轮转文件） |
| `temp/` | 临时文件（启动时会尝试清理 `data/temp`） |

---

## 打包与更新（Windows）

- 依赖中包含 `auto-py-to-exe`，可按需将 `gui_app.py` 打成独立可执行文件；打包后静态资源需与 `config` 中约定的目录结构一致（如 `bin/static` 或 `static`）。
- `services/update_service_win.py` 提供 **ZIP 覆盖更新**流程（下载 → 解压 → 校验含根级 `.exe` 与 `bin` 目录 → 批处理替换），**仅建议在 PyInstaller 冻结后的正式包上使用**；开发模式会直接拒绝以免破坏本地 Python 环境。

---

## 项目结构（简要）

```
qcsckp/
├── gui_app.py              # 程序入口：pywebview + 托盘 + 单实例
├── config.py               # 根路径、版本号、API 基址、SQLite 裁剪参数等
├── api/                    # 暴露给前端的 Python API（看盘、账号、服务控制）
├── services/               # 抓取主流程、飞书同步、Webhook、Windows 更新
├── utils/                  # SQLite、日志、通用工具、裁剪调度
├── static/                 # 前端页面与静态资源（HTML/JS/CSS）
├── hook/                   # 飞书机器人 Webhook 封装等
└── pyproject.toml          # 项目元数据与依赖
```

---

## 常见问题

1. **提示未检测到 Edge/Chrome**  
   在 Windows 上安装浏览器至默认路径，或后续在代码中扩展 `require_executable_path` 以支持自定义路径。

2. **无法启动采集**  
   需同时填写账号密码并通过远程校验；请确认网络可达 `API_BASE_URL` 且账号有效。

3. **数据库越来越大**  
   确认已开启 SQLite 裁剪环境变量，并按业务量调整 `SQLITE_PRUNE_MAX_ROWS`。

4. **飞书卡片没有发送或点击后不执行**
   先确认服务端已配置飞书自建应用、公开 HTTPS 回调、唯一授权 Open ID 和接收目标，再在桌面端重新登录以取得设备令牌。完整部署和验收步骤见 `../qcsckp-api-services/doc/FEISHU_RETARGET_SETUP.md`。

---

## 版本

应用内展示版本见 `config.CURRENT_VERSION`；`pyproject.toml` 中 `version` 为包元数据版本，二者可能不同步，以发布说明为准。

---

## 免责声明

本工具仅在**您有权访问的数据与账号范围内**使用。请遵守巨量千川、飞书等平台的服务条款与相关法律法规；开发者不对因滥用或违规使用产生的后果负责。
