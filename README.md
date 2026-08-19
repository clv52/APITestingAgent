# API Testing Agent

将接口 PDF 文档转换为可审阅、可执行的单接口边界测试。流程由 DeepSeek 的 OpenAI 兼容工具调用驱动：

1. 解析 PDF，得到 Markdown、`content_list` 和图片目录。
2. 根据 `content_list` 切分接口，生成每接口一个 Markdown。
3. 为每个接口生成符合统一 JSON schema 的边界测试用例，并在接口 Markdown 同级生成便于人工审阅的 Excel。
4. 对每个用例执行 HTTP 请求、断言响应，生成结果和 HTML 报告，并把状态、实际状态码、耗时和失败原因回填到对应 Excel。

项目同时提供命令行 Agent 与本地 REST Web 工作台。左侧先按任务展示一级目录，再在任务下按接口展示文件树；每个接口文件夹包含接口 Markdown、测试用例 JSON、测试用例 Excel 和测试结果。中间是可聊天、可读取当前任务文件并可实际调用测试流水线工具的 AI Agent 窗口，同时展示阶段、总进度和已完成子任务数。单击文件会把它设为对话上下文，双击会用本机默认程序打开。

如果准备系统学习源码，请先阅读 [CODE_READING_GUIDE.md](./CODE_READING_GUIDE.md)。其中给出了推荐文件顺序、两条主要调用链、关键函数和断点位置；核心源文件也已在重要状态流转处加入中文注释。

## 环境准备

- Python 3.10 或更高版本
- Node.js（用于生成 Excel 可视化文件）
- 已安装 `openai` 与 `requests`
- `.env` 中至少有 DeepSeek 配置：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

安装项目所需的 Python 依赖（包含 OpenAI SDK、HTTP 执行器和 MinerU）：

```powershell
python -m pip install -r .\requirements.txt
```

Excel 由 `@oai/artifact-tool` 生成。当前 Codex 工作区已经连接到随附的 Node 依赖；若迁移到独立环境，需要确保项目根目录能够解析该包，并可通过 `API_TEST_EXCEL_NODE` 指定 Node 可执行文件。

`requirements.txt` 已包含 MinerU。安装后程序默认从当前 Python 环境的 `Scripts/mineru.exe` 查找；也可以在启动时传入 `--mineru "C:\path\to\mineru.exe"`，或配置 `MINERU_PATH`。如果 `output` 下已有同名文档的解析结果，命令行流程可以安全复用它。

> 真实 HTTP 执行会向目标系统发出请求。请使用测试环境 URL 与无生产影响的测试账号/凭据。

## 启动 Web 页面

```powershell
python .\api_test_web.py --port 8000
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000) 后会直接进入工作台，不再经过单独的 PDF 上传页：

- 左侧把每个会话显示为同级一级目录；附加并处理 PDF 后，当前会话下会显示接口文件夹和报告（后端图片目录不在文件树中展示）。
- 点击当前任务名称可折叠其文件，再次点击可展开；点击其他任务名称会切换任务。
- 切换任务时会从后端恢复该任务已经保存的聊天记录，不会因新建任务而清空旧任务对话。
- 每个正式任务右侧提供重命名和删除按钮。重命名会写入后端 `task_meta.json`；删除经过确认后会删除该任务的聊天记录和全部后端产物。运行中的任务不能删除。
- 根地址会自动打开最近一次会话。如果没有历史会话，则显示一个空的“新任务”节点；直接输入文字即可创建可持久化的新会话，无需先上传 PDF。
- 点击聊天输入框左下角的“＋”附加 PDF，可同时输入自然语言任务要求，然后点击发送。
- 选择 PDF 后可在附件卡片中填写目标 API Base URL，也可以控制是否执行真实 HTTP 测试。
- 点击顶部“＋ 新任务”会在已有任务的同级位置增加“新任务”草稿节点，不会删除、覆盖或隐藏已有任务及聊天记录。发送第一条文字消息或上传 PDF 后，该草稿节点会转为正式会话。
- AI 回复支持 GitHub 风格 Markdown，包括标题、粗体、列表、引用、行内代码、代码块、链接和表格；用户消息仍按纯文本显示。Markdown 解析器随项目本地加载，不依赖 CDN，渲染前会经过标签和属性白名单过滤。

发送纯文字时，前端先以 JSON 调用 `POST /api/tasks` 创建会话，再调用 `POST /api/tasks/{id}/chat`；因此没有 PDF 也能正常聊天。PDF 可以在创建会话时上传，也可以通过 `POST /api/tasks/{id}/pdf` 后续附加到当前空会话。上传只保存附件，不会自动启动解析、切分、用例生成或测试；Agent 仅根据用户明确的聊天指令调用相应工具。

测试执行选项说明：

- 勾选“执行真实 HTTP 测试”：完成用例 schema 校验后，向该 Base URL 发送测试请求。
- 取消勾选：只校验每个接口的测试用例结构，不发送 HTTP 请求；结果页会显示已验证的接口用例数量。

任务产物保存到 `output/agent_ui_runs/<task-id>/run/`。Web 服务只绑定 `127.0.0.1`，不会主动暴露到局域网。

### 文件如何提供给前端和聊天 Agent

这里采用“后端任务工作区”方案，而不是在每次接口请求或聊天时重复上传文件：

1. 后端先为每个聊天窗口分配唯一 `task-id`；PDF 是可选附件，只上传一次且不会在后续聊天中重复传输。
2. PDF 解析、接口 Markdown、测试用例和测试结果都落到该任务的后端本地目录。
3. 左侧文件树请求 `/api/tasks/{id}/files`，只获取文件名、受限相对路径、大小和完成状态，不下载全部文件内容。
4. 单击文件时，前端只保存其相对路径并在聊天请求中发送 `task-id + message + selected_path`。
5. DeepSeek 聊天 Agent 拥有文件读取、受限文件操作和四阶段测试流水线工具。模型需要内容时，后端才在当前任务目录内读取对应片段；用户明确要求执行某个阶段时，模型会调用相应工具，而不是只给出操作建议。
6. 双击文件时，前端调用 `/api/tasks/{id}/files/open`；后端校验路径必须位于当前任务目录且类型在白名单中，然后调用 Windows 默认程序打开本地文件。
7. 每个任务的对话独立保存到该任务的 `run/chat_history.json`；切换一级任务节点时，前端通过 `/api/tasks/{id}/chat` 重新载入对应记录。

因此，左侧文件不会在每条消息中整体传给模型。前端发送的是 `task-id + message + selected_path`，后端 Agent 再按需读取该任务目录中的文件。这比把全部 Markdown、JSON 和图片反复塞进请求更快，也能保证模型读取到的是磁盘上的最新产物。

这种方式避免重复传输大 JSON/Markdown，保证聊天看到的始终是最新生成文件，也防止前端或模型读取任务目录以外的任意路径。`files/open` 面向当前这种本机 Web 服务；如果将后端部署到远程服务器，它打开的是服务器上的文件，此时应改为下载接口。

### 聊天 Agent 集成的工具

聊天 Agent 使用 OpenAI 兼容的 function calling 描述并交给 DeepSeek 决定是否调用：

| 工具 | 作用 |
| --- | --- |
| `parse_api_document` | 解析当前任务上传的 PDF，生成 Markdown、`content_list` 和 `images/` |
| `split_api_interfaces` | 对 `content_list` 做接口语义切分，生成每接口 Markdown，并保持 `images/...` 相对路径有效 |
| `generate_api_test_cases` | 为每个接口生成、校验边界用例 JSON，并自动导出 Excel 审阅文件 |
| `run_api_test_cases` | 解析运行时占位符、发送 HTTP 请求、执行断言，生成结果 JSON/HTML 并回填接口 Excel |
| `configure_test_environment` | 设置任务 Base URL、执行开关和可持久化的运行时变量 |
| `list_workspace_files` / `read_workspace_file` / `get_task_progress` | 查看任务文件和实时进度 |
| `write_workspace_file` / `copy_workspace_file` / `move_workspace_file` | 在用户明确要求时写入、复制或移动当前任务中的单个文件 |

四个阶段通常按“解析 → 切分 → 用例生成 → 自动化测试”执行，但已经有产物时也可以在聊天中单独重跑某一步。例如：

```text
请使用 http://127.0.0.1:9001 执行当前任务的自动化测试。
OAUTH_CLIENT_ID=mock-client-id，OAUTH_CLIENT_SECRET=...，ACCESS_TOKEN=...
```

执行真实请求前，Agent 必须拿到用户明确提供的 `http`/`https` Base URL，不会擅自使用文档中的公网示例地址。Base URL、执行开关以及 OAuth 密钥、Token 等运行时变量都会原样保存到任务的 `task_meta.json`；聊天记录、工具返回值和测试结果也不再脱敏，因此后端重启后仍可继续测试。请只在本机受控测试环境使用，并妥善保护 `output/agent_ui_runs/`，不要提交到版本库或发送给无关人员。

自动化测试采用全量预检：后端先汇总所有接口用例的 `required_env`，检查 Base URL 和每个运行时变量。只要缺少一项，`run_api_test_cases` 就返回 `blocked`、`needs_user_input=true` 和准确的 `missing_env`，不发送任何 HTTP 请求，也不写入一批 `skipped` 结果；聊天 Agent 会据此向用户逐项询问。用户补齐后，Agent 才重新调用工具并执行完整测试集。

文件变更工具只接受相对于当前任务 `run/` 的路径，禁止绝对路径和 `../` 越界；复制、移动默认禁止覆盖已有文件。Agent 仅在用户明确提出文件修改要求时使用这些工具。

`POST /api/tasks/{id}/chat` 会等待本条 Agent 调用完成后返回答案。后端使用多线程 HTTP 服务，因此工具执行期间前端仍能轮询任务进度和文件树，不会阻塞其他状态请求。

## REST API

| 方法 | 地址 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务健康检查 |
| `POST` | `/api/tasks` | 以 JSON 创建无 PDF 会话，或以 PDF 原始二进制创建带附件会话；均不会自动运行流水线 |
| `GET` | `/api/tasks` | 当前进程中的任务列表 |
| `GET` | `/api/tasks/{id}` | 任务状态与每一步进度 |
| `PATCH` | `/api/tasks/{id}` | 修改会话名称并持久化到任务元数据 |
| `DELETE` | `/api/tasks/{id}` | 删除已结束任务及其全部后端文件 |
| `GET` | `/api/tasks/{id}/interfaces` | 切分后的接口 Markdown |
| `GET` | `/api/tasks/{id}/test-cases` | 每接口的测试用例 JSON |
| `GET` | `/api/tasks/{id}/results` | 测试指标、逐条结果和异常项 |
| `GET` | `/api/tasks/{id}/report` | 完整静态 HTML 报告（真实执行后） |
| `GET` | `/api/tasks/{id}/files` | 左侧按接口组织的虚拟文件树（不返回后端 images 列表） |
| `GET` | `/api/tasks/{id}/files/content?path=...` | 读取任务内文本文件（保留给 API/调试使用） |
| `POST` | `/api/tasks/{id}/files/open` | 校验相对路径后用本机默认程序打开文件 |
| `POST` | `/api/tasks/{id}/pdf` | 给尚无 PDF 的现有会话附加接口文档，不自动执行流水线 |
| `GET` | `/api/tasks/{id}/chat` | 获取当前任务的持久化聊天记录 |
| `POST` | `/api/tasks/{id}/chat` | 发送聊天消息和可选的左侧文件上下文 |

`POST /api/tasks` 使用 `application/json`（如 `{}`）时创建纯聊天会话；使用 `application/pdf` 时创建带附件会话。`POST /api/tasks/{id}/pdf` 也使用 `application/pdf`。PDF 上传支持以下请求头：

| 请求头 | 必填 | 含义 |
| --- | --- | --- |
| `X-Filename` | 否 | URI 编码后的原始文件名 |
| `X-API-Base-URL` | 否 | 本次任务专用的目标 Base URL |
| `X-Execute-Tests` | 否 | `true`（默认）执行请求，`false` 仅校验 |

PowerShell 示例（仅校验，不向目标系统发请求）：

```powershell
$pdf = ".\asset\华为云OrgID_API接口说明文档_中文版.pdf"
$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $pdf))
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks" `
  -ContentType "application/pdf" `
  -Headers @{ "X-Filename" = [uri]::EscapeDataString((Split-Path $pdf -Leaf)); "X-Execute-Tests" = "false" } `
  -Body $bytes
```

使用返回的 `id` 轮询任务：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/tasks/<task-id>"
```

聊天请求只传消息和可选的左侧文件相对路径：

```json
{
  "message": "分析这个接口的边界覆盖是否充分",
  "selected_path": "test_cases/001_xxx_cases.json"
}
```

双击文件对应的打开请求同样只传受限相对路径：

```json
{
  "path": "interfaces_markdown/001_xxx.md"
}
```

后端不会接受绝对路径或 `../` 越界路径。

## 命令行 Agent

```powershell
python .\api_test_agent.py `
  --pdf ".\asset\华为云OrgID_API接口说明文档_中文版.pdf" `
  --base-url "https://api.example.test"
```

也可以让 Agent 从自然语言中识别路径：

```powershell
python .\api_test_agent.py --prompt "PDF 在 .\asset\华为云OrgID_API接口说明文档_中文版.pdf，请执行接口测试"
```

## 图片与 Markdown 层级

解析阶段将 Markdown 与图片一起放入：

```text
run/parsed/
├── 文档.md
└── images/
```

接口切分阶段会将每接口 Markdown 与同一份图片目录一起放入：

```text
run/interfaces_markdown/
├── 001_xxx.md
├── 001_xxx_测试用例.xlsx
├── 002_xxx.md
├── 002_xxx_测试用例.xlsx
└── images/
```

所以 Markdown 中原有的 `images/xxx.png` 相对引用始终有效；程序不会把图片路径改成绝对路径，也不会改写源 Markdown。

前端不保存图片副本，也不再在左侧文件树显示或接收 `images` 文件列表。图片只保留在后端 `run/interfaces_markdown/images/`（或解析目录）中，供 Markdown 相对引用和后端 Agent 处理使用。

### 左侧接口文件夹是怎样形成的

左侧每个接口文件夹是后端生成的“虚拟视图”，它把已有产物按接口编号关联起来：

```text
001_接口名称/
├── 接口文档.md       -> run/interfaces_markdown/001_xxx.md
├── 测试用例.json     -> run/test_cases/001_xxx_cases.json
├── 测试用例.xlsx     -> run/interfaces_markdown/001_xxx_测试用例.xlsx
└── 测试结果.json     -> run/test_results/001_xxx_cases_results.json
```

Excel 是从 JSON 自动导出的审阅视图：每行对应一条测试用例，包含清晰的用例描述、场景分类、边界目标、请求参数和响应断言。自动化测试完成后，会按用例 ID 回填“执行状态、实际状态码、响应时间、失败/异常原因”；失败和错误行显示为红色，跳过行显示为黄色，通过状态显示为绿色，“用例概览”同步统计各执行状态数量。自动化测试始终读取 JSON；直接编辑 Excel 不会改变执行数据。若 Excel 正被本机程序打开，Windows 会阻止回填，关闭该工作簿后重新执行测试即可。

## 结果指标

- **测试总数**：实际得到执行结果的 case 数。
- **通过率**：`passed / (total - skipped)`；没有可执行 case 时显示为 `—`。
- **执行率**：`(total - skipped) / total`。
- **异常 / 失败**：断言不符合预期的 `failed` 与运行时 `error` 数量；可在左侧测试结果 JSON 和完整 HTML 报告中查看。
- **平均/最慢响应时间**：成功收到 HTTP 响应的 case 的耗时统计。
- **仅校验模式**：显示通过 schema 校验的接口用例文件数量，明确标注没有发送 HTTP 请求。

## 主要文件

- `api_test_agent.py`：唯一的 DeepSeek Agent 核心，包含普通聊天、OpenAI tool-calling 循环、共享工具描述、`PipelineLayout`、`PipelineState`、`ApiTestToolHost` 和可复用的 `run_pdf_pipeline`。
- `api_test_web.py`：标准库 REST/静态文件服务、持久化会话管理、上传与前端进度适配；通过 `ApiTestAgent.chat()` 使用 Agent，不再维护第二套模型循环。
- `webapp/`：无前端依赖的单页界面。
- `utils/parse_pdf.py`：PDF 解析。
- `utils/content_list_to_md.py`、`utils/llm_split_interfaces.py`：接口切分。
- `utils/generate_api_test_cases.py`：边界测试用例生成。
- `utils/export_test_cases_excel.py`、`utils/export_test_cases_excel.mjs`：将每接口用例 JSON 导出为同级 Excel 可视化文件。
- `utils/run_api_test_cases.py`、`utils/visualize_api_results.py`：用例执行与报告。

依赖方向固定为：`webapp → api_test_web.py → api_test_agent.py → utils`。Web 和 Agent 通过同一个 `PipelineLayout(run_dir)` 计算 `parsed/`、`interfaces_markdown/`、`test_cases/`、`test_results/` 等路径，因此前端文件树展示的就是 Agent 实际落盘文件；`images/` 保留在后端同级目录供 Markdown 相对引用，但不发送到前端文件树。
