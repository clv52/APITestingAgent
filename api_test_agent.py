"""DeepSeek tool-calling agent for the PDF-to-API-test workflow.

The agent exposes file-location/read/write tools plus the existing utility
steps as OpenAI function tools. The model chooses the order and arguments,
while this host owns the active PDF path and generated artifact paths.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
UTILS_DIR = PROJECT_ROOT / "utils"
if str(UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(UTILS_DIR))

import generate_api_test_cases
import llm_split_interfaces
import parse_pdf
import run_api_test_cases
from api_test_case_spec import atomic_write_json, load_env_file
from export_test_cases_excel import update_case_workbook_results
from run_api_test_cases import write_html_report


# 这是“命令行完整流水线”的提示词，会主动跑四阶段；Web 聊天使用
# 本文件中更保守的 CHAT_SYSTEM_PROMPT，两种策略共用同一个 ApiTestAgent 循环。
SYSTEM_PROMPT = """You are an API-test pipeline orchestrator.

Step 0: resolve the PDF location from the user's message.
- If the user gives a PDF file path, call locate_api_document with that path.
- If the user gives a directory or is vague, use list_files to inspect it and
  locate_api_document to select the PDF.
- Use read_text_file only to inspect Markdown/JSON/TXT artifacts; do not read
  binary PDFs with it. Never invent a path that the tools did not return.

After a real PDF path is active, process exactly one PDF through this order:
1. parse_api_document
2. split_api_interfaces
3. generate_api_test_cases
4. run_api_test_cases with execute=true

Use tools, never claim a step succeeded without its tool result. Each tool owns
the paths for the current run; do not invent or request arbitrary generated
paths. After each result, inspect its status. If a tool returns ok=false,
explain the exact blocker and stop. Do not call a later pipeline stage after a
prerequisite failed.
The runner returns ok=true whenever execution completed, even when some test
cases failed or errored; treat those as normal test outcomes and include them in
the summary. The runner also generates an HTML report with metrics and abnormal
cases; include its path in the final report.

The parser stages Markdown, content_list and images together. Do not ask to
rewrite Markdown image links. The runner may report blocked when API_BASE_URL or
runtime credentials are unavailable; report that truthfully as the final state.
When all available stages have completed, give a concise Chinese summary with
the generated artifact paths and test counts.
"""


# OpenAI function-calling 描述层：这里只告诉模型工具签名，真正实现位于
# ApiTestToolHost.handlers 白名单中。描述和实现必须保持同名、同参数。
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "locate_api_document",
            "description": "Resolve the PDF path the user described. Accept a PDF file path or a directory. If a directory is given, search for PDF files and set the active PDF when exactly one is found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "PDF file path or directory path from the user's message.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Search subdirectories when path is a directory. Default false.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories, useful when the user gives a folder and you need to find the PDF.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to inspect.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern such as *.pdf. Default matches all entries.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Search subdirectories recursively. Default false.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum entries to return. Default 100.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "Read a UTF-8/GBK text file such as Markdown, JSON, TXT or LOG. Binary files and PDFs are rejected. Content is returned unchanged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Text file path.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 200000,
                        "description": "Maximum characters to return. Default 20000.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_text_file",
            "description": "Write a text file under the project's output directory. Overwrite is disabled by default; use append or force explicitly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Output file path under the project's output directory.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "Append instead of overwrite. Default false.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Allow overwriting an existing file. Default false.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_api_document",
            "description": "Parse the active PDF into Markdown, MinerU content_list JSON and an images directory. If no PDF is active, pdf_path must be supplied. By default, safely reuse already-existing MinerU artifacts for the same PDF; all artifacts are staged together so relative images/... Markdown links remain valid.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pdf_path": {
                        "type": "string",
                        "description": "Optional PDF file path when locate_api_document was not used or the active PDF should change.",
                    },
                    "reuse_existing": {
                        "type": "boolean",
                        "description": "Reuse an existing parse result for this exact PDF if present. Default true.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "ocr", "txt"],
                        "description": "MinerU parse mode when a new parse is necessary. Default auto.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "split_api_interfaces",
            "description": "Use DeepSeek to locate real API boundaries in the parsed content_list, write one JSON slice per API, and render one Markdown file per interface. The tool places a sibling images directory beside generated Markdown without rewriting image paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "strict_markdown": {
                        "type": "boolean",
                        "description": "Fail on an unknown MinerU block type while rendering interface Markdown. Default false.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_api_test_cases",
            "description": "For every interface Markdown produced by the split tool, use DeepSeek to generate validated declarative boundary-test JSON. The model generates data only; no executable test code is accepted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_images": {
                        "type": "boolean",
                        "description": "Send local images referenced by the interface Markdown to the case-generation model. Default true.",
                    },
                    "skip_existing": {
                        "type": "boolean",
                        "description": "Skip a case JSON file that already exists. Default false.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_api_test_cases",
            "description": "Validate and execute every generated test-case JSON against the configured API base URL, then generate result JSON, update each interface Excel workbook, and create an HTML report. This can issue real HTTP requests only when execute=true. Values are recorded unchanged. API_BASE_URL and credential variables must be configured.",
            "parameters": {
                "type": "object",
                "properties": {
                    "execute": {
                        "type": "boolean",
                        "description": "Must be true for the final pipeline stage so real HTTP requests are attempted.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120000,
                        "description": "Optional per-request timeout override in milliseconds.",
                    },
                },
                "required": ["execute"],
                "additionalProperties": False,
            },
        },
    },
]


PIPELINE_TOOL_NAMES = frozenset({
    "parse_api_document",
    "split_api_interfaces",
    "generate_api_test_cases",
    "run_api_test_cases",
})

WORKSPACE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "列出当前任务工作区中按接口分组的 Markdown、测试用例、Excel、测试结果和报告。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "读取当前任务 run 工作区中的文本文件。路径必须相对于 run 目录，并支持分页读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "任务 run 目录内的相对路径"},
                    "offset": {"type": "integer", "minimum": 0, "description": "字符起始位置，默认 0"},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000, "description": "本次最多读取字符数，默认 20000"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_progress",
            "description": "获取当前 PDF 解析、接口切分、用例生成和自动化测试的实时进度。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "仅按用户明确要求，在当前任务 run 工作区内写入文本文件；默认禁止覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于任务 run 目录的目标路径"},
                    "content": {"type": "string", "description": "UTF-8 文本内容"},
                    "append": {"type": "boolean", "description": "是否追加，默认 false"},
                    "overwrite": {"type": "boolean", "description": "是否允许覆盖，默认 false"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_workspace_file",
            "description": "按用户明确要求，在当前任务 run 工作区内复制单个文件；不覆盖已有目标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "相对于 run 的源文件路径"},
                    "destination": {"type": "string", "description": "相对于 run 的目标文件路径"},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_workspace_file",
            "description": "仅当用户明确要求移动或重命名时，在当前任务 run 工作区内移动单个文件；不覆盖已有目标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "相对于 run 的源文件路径"},
                    "destination": {"type": "string", "description": "相对于 run 的目标文件路径"},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_test_environment",
            "description": "设置当前任务的 HTTP Base URL、执行开关和运行时环境变量，供后续自动化测试使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_url": {"type": "string", "description": "用户明确提供的 http/https API Base URL"},
                    "execute_requests": {"type": "boolean", "description": "是否允许发送真实 HTTP 请求，默认 true"},
                    "environment": {
                        "type": "object",
                        "description": "测试运行时变量，例如 OAuth 凭据、Token、授权码或票据",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["base_url"],
                "additionalProperties": False,
            },
        },
    },
]


def _build_chat_pipeline_tools() -> list[dict[str, Any]]:
    """复用公共四阶段工具 schema，并为 Web 测试入口增加环境配置参数。"""
    tools = [
        copy.deepcopy(tool)
        for tool in TOOLS
        if tool["function"]["name"] in PIPELINE_TOOL_NAMES
    ]
    parse_tool = next(tool for tool in tools if tool["function"]["name"] == "parse_api_document")
    # Web 只能处理当前会话已经附加的 PDF，不能允许模型覆盖为任意本机路径。
    parse_properties = parse_tool["function"]["parameters"]["properties"]
    parse_properties.pop("pdf_path", None)
    parse_properties["reuse_existing"]["description"] = "复用当前会话已有解析产物；首次解析默认 false"
    run_tool = next(tool for tool in tools if tool["function"]["name"] == "run_api_test_cases")
    properties = run_tool["function"]["parameters"]["properties"]
    properties.update(
        {
            "base_url": {"type": "string", "description": "可选；覆盖当前任务的 API Base URL"},
            "environment": {
                "type": "object",
                "description": "本次测试需要补充的运行时变量",
                "additionalProperties": {"type": "string"},
            },
        }
    )
    return tools


CHAT_TOOLS: list[dict[str, Any]] = [*WORKSPACE_TOOLS, *_build_chat_pipeline_tools()]

CHAT_SYSTEM_PROMPT = """你是当前 API 测试任务的可执行 Agent，同时也是普通聊天助手。请使用中文清晰回答。

行为边界：
1. 没有 PDF 时也必须正常聊天、解释代码、讨论方案；只有 PDF 流水线工具需要附件。
2. 上传或附加 PDF 本身不等于授权执行。只有用户明确要求解析、切分、生成用例或执行测试时，才调用对应工具。
3. 用户说停止、取消、暂不执行时，不得继续调用任何流水线工具。
4. 用户询问工作区事实、文件内容、用例数量或测试结果时，先调用读取类工具，不得猜测。
5. 有能够直接满足用户请求的工具时应调用工具，不得谎称需要其他执行组件接管。

流水线关系：
- 通常顺序为 parse_api_document → split_api_interfaces → generate_api_test_cases → run_api_test_cases。
- 可以回答普通问题，也可以只执行用户明确指定的一个阶段；缺少前置产物时说明并询问是否补跑前置阶段。
- 不要擅自重复已经完成的阶段，除非用户明确要求重新生成或重新测试。
- Markdown 与 images 的目录关系由工具维护，不要改写文档中的 images/... 相对路径。

自动化测试安全要求：
- 发送真实 HTTP 请求前必须获得用户明确提供的 Base URL，不得采用文档示例公网地址。
- 可以用 configure_test_environment 保存 Base URL 和运行时变量，也可以把配置随 run_api_test_cases 一起传入。
- run_api_test_cases 会对整批 required_env 做预检。若返回 needs_user_input=true 或 missing_env，立即停止工具调用，并逐项询问用户；不得生成部分 skipped 结果。
- 运行时变量按用户提供的原值保存和注入，不要自行脱敏或替换。
- 本地 mock 测试结果只代表 mock 行为，不代表真实接口质量。

文件工具要求：
- 所有路径都相对于当前任务 run 工作区。
- 只有用户明确要求写入、复制、移动或重命名文件时，才能调用相应变更工具。
- 回答生成结果时说明关键文件及当前完成状态，不要编造不存在的路径。
"""


class AgentError(RuntimeError):
    """Raised for an invalid agent configuration or a failed tool host."""


@dataclass(frozen=True)
class PipelineLayout:
    """统一定义一次任务的产物目录，供 Agent 落盘和 Web 展示共同使用。"""

    run_dir: Path

    @property
    def mineru_raw_dir(self) -> Path:
        """返回 MinerU 未整理原始输出目录。"""
        return self.run_dir / "mineru_raw"

    @property
    def parsed_dir(self) -> Path:
        """返回稳定的 PDF 解析产物目录。"""
        return self.run_dir / "parsed"

    @property
    def parse_manifest(self) -> Path:
        """返回 PDF 解析清单路径。"""
        return self.parsed_dir / "parse_manifest.json"

    @property
    def split_dir(self) -> Path:
        """返回接口边界切分中间产物目录。"""
        return self.run_dir / "split"

    @property
    def split_manifest(self) -> Path:
        """返回接口切分清单路径。"""
        return self.split_dir / "split_manifest.json"

    @property
    def interfaces_dir(self) -> Path:
        """返回接口 Markdown、同级 Excel 与 images 所在目录。"""
        return self.run_dir / "interfaces_markdown"

    @property
    def interface_images_dir(self) -> Path:
        """返回接口 Markdown 通过 images/... 相对引用的图片目录。"""
        return self.interfaces_dir / "images"

    @property
    def cases_dir(self) -> Path:
        """返回声明式测试用例 JSON 目录。"""
        return self.run_dir / "test_cases"

    @property
    def case_manifest(self) -> Path:
        """返回测试用例生成清单路径。"""
        return self.cases_dir / "generation_manifest.json"

    @property
    def results_dir(self) -> Path:
        """返回自动化测试结果与 HTML 报告目录。"""
        return self.run_dir / "test_results"

    @property
    def run_manifest(self) -> Path:
        """返回自动化测试汇总清单路径。"""
        return self.results_dir / "run_manifest.json"

    @property
    def report(self) -> Path:
        """返回自包含 HTML 测试报告路径。"""
        return self.results_dir / "api_test_report.html"

    @property
    def chat_history(self) -> Path:
        """返回 Web 会话聊天历史路径。"""
        return self.run_dir / "chat_history.json"

    @property
    def agent_trace(self) -> Path:
        """返回 Agent 工具轨迹与最后摘要路径。"""
        return self.run_dir / "agent_trace.json"


@dataclass
class PipelineState:
    """一次流水线的可信状态；各阶段通过这里传递产物路径，而非让模型猜路径。"""
    pdf_path: Path | None
    run_dir: Path
    env_file: Path
    mineru_path: Path | None
    base_url: str | None
    execute_requests: bool = True
    runtime_env: dict[str, str] = field(default_factory=dict)
    parse_artifacts: dict[str, str] | None = None
    split_manifest: Path | None = None
    interface_markdown_dir: Path | None = None
    cases_dir: Path | None = None
    case_manifest: Path | None = None
    test_results_dir: Path | None = None
    tool_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def layout(self) -> PipelineLayout:
        """返回与当前 run_dir 绑定的共享产物目录布局。"""
        return PipelineLayout(self.run_dir)


def _inside_project(path: Path) -> Path:
    """解析路径并确保它位于项目目录内，防止 Agent 越权写入。"""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise AgentError(f"Path must stay inside this project: {resolved}") from error
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    """判断一个解析后的路径是否位于指定根目录之下。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_user_path(raw: Any) -> Path:
    """将用户传入的绝对或相对路径解析为本机绝对路径。"""
    text = str(raw).strip().strip("\"'")
    if not text:
        raise AgentError("Path must not be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        candidates = [PROJECT_ROOT / path, Path.cwd() / path]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.exists():
                return resolved
        path = PROJECT_ROOT / path
    return path.resolve()


def _read_text_content(path: Path, max_chars: int) -> tuple[str, bool]:
    """安全读取小型文本文件，并返回内容以及是否被截断。"""
    if not path.is_file():
        raise AgentError(f"File does not exist: {path}")
    if path.stat().st_size > 5 * 1024 * 1024:
        raise AgentError(f"File is too large to read as text: {path}")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise AgentError(f"Binary file is not readable as text: {path}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030", errors="replace")
    return text[:max_chars], len(text) > max_chars


def _bool(arguments: Mapping[str, Any], key: str, default: bool) -> bool:
    """从工具参数中读取并严格校验一个布尔值。"""
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise AgentError(f"Tool argument {key} must be boolean")
    return value


def _optional_positive_int(arguments: Mapping[str, Any], key: str) -> int | None:
    """读取可选的 1~120000 正整数工具参数。"""
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 120000:
        raise AgentError(f"Tool argument {key} must be an integer between 1 and 120000")
    return value


def _check_arguments(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    """拒绝工具定义白名单之外的参数。"""
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise AgentError(f"Unexpected tool arguments: {', '.join(unexpected)}")


def _json_result(ok: bool, status: str, **values: Any) -> dict[str, Any]:
    """构造所有 Agent 工具共用的结构化返回值。"""
    return {"ok": ok, "status": status, **values}


def _copy_file(source: Path, destination: Path) -> None:
    """创建目标父目录，并保留元数据复制单个文件。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _file_entry(path: Path) -> dict[str, Any]:
    """把本地文件或目录转换为 Agent 可消费的目录项信息。"""
    return {
        "name": path.name,
        "path": str(path),
        "type": "dir" if path.is_dir() else "file",
        "size": path.stat().st_size if path.is_file() else None,
        "is_pdf": path.is_file() and path.suffix.lower() == ".pdf",
    }


def _stage_artifacts(state: PipelineState, artifacts: Mapping[str, Path | None]) -> dict[str, str]:
    """把解析产物复制进本次 run，并保持 Markdown/images 的相对层级。"""
    markdown = artifacts.get("markdown")
    content_list = artifacts.get("content_list")
    images_dir = artifacts.get("images_dir")
    if not isinstance(markdown, Path) or not markdown.is_file():
        raise AgentError("PDF parse did not produce Markdown")
    if not isinstance(content_list, Path) or not content_list.is_file():
        raise AgentError("PDF parse did not produce a MinerU content_list JSON file")

    destination = state.layout.parsed_dir
    if destination.exists():
        raise AgentError(f"Parse staging directory already exists: {destination}")
    destination.mkdir(parents=True)
    staged_markdown = destination / markdown.name
    staged_content_list = destination / content_list.name
    _copy_file(markdown, staged_markdown)
    _copy_file(content_list, staged_content_list)
    staged_images = destination / "images"
    image_count = 0
    if isinstance(images_dir, Path) and images_dir.is_dir():
        shutil.copytree(images_dir, staged_images)
        image_count = sum(1 for item in staged_images.rglob("*") if item.is_file())
    else:
        staged_images.mkdir()
    manifest = {
        "pdf": str(state.pdf_path) if state.pdf_path else "",
        "markdown": str(staged_markdown),
        "content_list": str(staged_content_list),
        "images_dir": str(staged_images),
        "image_count": image_count,
        "image_links_are_relative_to_markdown": True,
    }
    atomic_write_json(destination / "parse_manifest.json", manifest)
    return manifest


class ApiTestToolHost:
    """A path-restricted wrapper around the deterministic utility modules."""

    def __init__(
        self,
        state: PipelineState,
        on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """绑定流水线状态、进度回调以及允许模型调用的工具白名单。"""
        self.state = state
        self.on_tool_result = on_tool_result
        self.handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "locate_api_document": self.locate_api_document,
            "list_files": self.list_files,
            "read_text_file": self.read_text_file,
            "write_text_file": self.write_text_file,
            "parse_api_document": self.parse_api_document,
            "split_api_interfaces": self.split_api_interfaces,
            "generate_api_test_cases": self.generate_api_test_cases,
            "run_api_test_cases": self.run_api_test_cases,
        }

    def execute(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """统一工具入口：白名单分发、异常结构化、历史与进度事件记录。"""
        if self.on_tool_result is not None:
            try:
                self.on_tool_result({"event": "started", "tool": name, "arguments": dict(arguments)})
            except Exception:
                pass
        handler = self.handlers.get(name)
        if handler is None:
            result = _json_result(False, "failed", error=f"Unknown tool: {name}")
        else:
            try:
                result = handler(arguments)
            except Exception as error:  # Tool errors are returned to the model, never hidden.
                result = _json_result(False, "failed", error=f"{type(error).__name__}: {error}")
        entry = {"event": "completed", "tool": name, "arguments": dict(arguments), "result": result}
        self.state.tool_history.append(entry)
        if self.on_tool_result is not None:
            try:
                self.on_tool_result(entry)
            except Exception:
                # UI progress reporting must never alter the actual pipeline result.
                pass
        return result

    def locate_api_document(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """定位用户指定的 PDF 文件，并将其设为当前活动接口文档。"""
        _check_arguments(arguments, {"path", "recursive"})
        recursive = _bool(arguments, "recursive", False)
        path = _resolve_user_path(arguments["path"])
        if path.is_file():
            if path.suffix.lower() != ".pdf":
                return _json_result(False, "not_a_pdf", path=str(path), error="The file exists but is not a PDF")
            self.state.pdf_path = path
            return _json_result(
                True,
                "found",
                pdf_path=str(path),
                file_size=path.stat().st_size,
                message="Active PDF path is set.",
            )
        if path.is_dir():
            pdfs = sorted(path.rglob("*.pdf")) if recursive else sorted(path.glob("*.pdf"))
            if not pdfs:
                return _json_result(
                    False,
                    "not_found",
                    searched=str(path),
                    recursive=recursive,
                    error="No PDF file was found in this directory",
                )
            if len(pdfs) == 1:
                self.state.pdf_path = pdfs[0]
                return _json_result(
                    True,
                    "found",
                    pdf_path=str(pdfs[0]),
                    file_size=pdfs[0].stat().st_size,
                    message="Active PDF path is set.",
                )
            return _json_result(
                True,
                "multiple",
                searched=str(path),
                pdfs=[str(pdf) for pdf in pdfs[:20]],
                message="Multiple PDF files were found; choose one and call locate_api_document with its full path.",
            )
        return _json_result(False, "not_found", path=str(path), error="The resolved path does not exist")

    def list_files(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """按模式列出指定目录中的文件，供 Agent 发现工作区产物。"""
        _check_arguments(arguments, {"path", "pattern", "recursive", "max_results"})
        path = _resolve_user_path(arguments["path"])
        if not path.exists():
            return _json_result(False, "not_found", path=str(path), error="The resolved path does not exist")
        recursive = _bool(arguments, "recursive", False)
        max_results = arguments.get("max_results", 100)
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 500:
            raise AgentError("Tool argument max_results must be an integer between 1 and 500")
        pattern = str(arguments.get("pattern") or "*")
        entries: list[dict[str, Any]] = []
        if path.is_file():
            entries.append(_file_entry(path))
        else:
            iterator = path.rglob(pattern) if recursive else path.glob(pattern)
            for item in sorted(iterator, key=lambda value: value.name.casefold()):
                if item.is_file() or item.is_dir():
                    entries.append(_file_entry(item))
                if len(entries) >= max_results:
                    break
        return _json_result(
            True,
            "completed",
            path=str(path),
            entry_count=len(entries),
            entries=entries,
        )

    def read_text_file(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """读取 Agent 指定的文本文件，并对大小和返回字符数设限。"""
        _check_arguments(arguments, {"path", "max_chars"})
        path = _resolve_user_path(arguments["path"])
        max_chars = arguments.get("max_chars", 20000)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 100 <= max_chars <= 200000:
            raise AgentError("Tool argument max_chars must be an integer between 100 and 200000")
        content, truncated = _read_text_content(path, max_chars)
        return _json_result(
            True,
            "completed",
            path=str(path),
            file_size=path.stat().st_size,
            truncated=truncated,
            content=content,
        )

    def write_text_file(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """在当前任务或项目 output 范围内新建、覆盖或追加文本文件。"""
        _check_arguments(arguments, {"path", "content", "append", "force"})
        path = _resolve_user_path(arguments["path"])
        if not (_is_within(path, PROJECT_ROOT / "output") or _is_within(path, self.state.run_dir)):
            return _json_result(
                False,
                "blocked",
                path=str(path),
                error="write_text_file is only allowed under the project output directory or the current run directory",
            )
        append = _bool(arguments, "append", False)
        force = _bool(arguments, "force", False)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise AgentError("Tool argument content must be a string")
        if path.exists() and not append and not force:
            return _json_result(False, "exists", path=str(path), error="File exists; pass force=true to overwrite or append=true")
        path.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return _json_result(True, "completed", path=str(path), bytes_written=len(content.encode("utf-8")))

    def parse_api_document(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """阶段 1：复用已有 MinerU 产物或重新解析，再暂存到当前 run。"""
        _check_arguments(arguments, {"pdf_path", "reuse_existing", "mode"})
        if self.state.parse_artifacts:
            return _json_result(True, "already_completed", artifacts=self.state.parse_artifacts)
        if arguments.get("pdf_path"):
            candidate = _resolve_user_path(arguments["pdf_path"])
            if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
                raise AgentError(f"pdf_path must be an existing PDF file: {candidate}")
            self.state.pdf_path = candidate
        if self.state.pdf_path is None:
            return _json_result(
                False,
                "blocked",
                error="No PDF path is active. Call locate_api_document with the path from the user, or pass pdf_path.",
            )
        reuse_existing = _bool(arguments, "reuse_existing", True)
        mode = arguments.get("mode", "auto")
        if mode not in {"auto", "ocr", "txt"}:
            raise AgentError("Tool argument mode must be auto, ocr or txt")

        source: dict[str, Path | None] | None = None
        source_kind = "new_parse"
        if reuse_existing:
            try:
                source = parse_pdf._find_artifacts(self.state.pdf_path, PROJECT_ROOT / "output")
                if source.get("content_list") is not None:
                    source_kind = "reused_existing_parse"
                else:
                    source = None
            except FileNotFoundError:
                source = None
        if source is None:
            raw_output = self.state.layout.mineru_raw_dir
            parse_pdf.parse_pdf(
                self.state.pdf_path,
                raw_output,
                mineru_path=self.state.mineru_path,
                mode=mode,
            )
            source = parse_pdf._find_artifacts(self.state.pdf_path, raw_output)

        staged = _stage_artifacts(self.state, source)
        self.state.parse_artifacts = staged
        return _json_result(True, source_kind, artifacts=staged)

    def split_api_interfaces(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """阶段 2：让模型识别语义边界，本地校验后生成每接口 Markdown。"""
        _check_arguments(arguments, {"strict_markdown"})
        if self.state.split_manifest:
            return _json_result(True, "already_completed", manifest=str(self.state.split_manifest))
        if not self.state.parse_artifacts:
            return _json_result(False, "blocked", error="parse_api_document must complete first")
        strict_markdown = _bool(arguments, "strict_markdown", False)
        content_list_path = _inside_project(Path(self.state.parse_artifacts["content_list"]))
        content_list = llm_split_interfaces.load_content_list(str(content_list_path))
        llm_view = llm_split_interfaces.build_llm_view(content_list)
        if not llm_view.strip():
            return _json_result(False, "failed", error="Parsed content_list has no usable interface text")

        split_dir = self.state.layout.split_dir
        markdown_dir = self.state.layout.interfaces_dir
        source_images = _inside_project(Path(self.state.parse_artifacts["images_dir"]))
        markdown_dir.mkdir(parents=True, exist_ok=False)
        shutil.copytree(source_images, markdown_dir / "images")
        (split_dir / "llm_boundary_view.txt").parent.mkdir(parents=True, exist_ok=True)
        (split_dir / "llm_boundary_view.txt").write_text(llm_view, encoding="utf-8")

        client = llm_split_interfaces.create_client()
        raw_result = llm_split_interfaces.call_deepseek(client, llm_view)
        atomic_write_json(split_dir / "deepseek_raw_result.json", raw_result)
        apis = llm_split_interfaces.validate_boundaries(raw_result, content_list)
        for api in apis:
            llm_split_interfaces.verify_api_content(api, content_list)
        if not apis:
            return _json_result(False, "failed", error="DeepSeek did not identify a valid API boundary")
        manifest = llm_split_interfaces.save_split_results(
            apis=apis,
            content_list=content_list,
            output_dir=split_dir,
            markdown_output_dir=markdown_dir,
            strict_markdown=strict_markdown,
        )
        self.state.split_manifest = manifest.resolve()
        self.state.interface_markdown_dir = markdown_dir.resolve()
        return _json_result(
            True,
            "completed",
            api_count=len(apis),
            manifest=str(self.state.split_manifest),
            interface_markdown_dir=str(self.state.interface_markdown_dir),
            markdown_images_dir=str(markdown_dir / "images"),
        )

    def generate_api_test_cases(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """阶段 3：逐接口调用模型，输出通过 schema 校验的 JSON 和 Excel。"""
        _check_arguments(arguments, {"include_images", "skip_existing"})
        if self.state.case_manifest:
            return _json_result(True, "already_completed", manifest=str(self.state.case_manifest))
        if not self.state.interface_markdown_dir:
            return _json_result(False, "blocked", error="split_api_interfaces must complete first")
        include_images = _bool(arguments, "include_images", True)
        skip_existing = _bool(arguments, "skip_existing", False)
        markdown_files = generate_api_test_cases.discover_markdown(
            self.state.interface_markdown_dir,
            "[0-9][0-9][0-9]_*.md",
        )
        api_key, base_url, model = generate_api_test_cases._model_config(self.state.env_file)
        try:
            from openai import OpenAI
        except ImportError as error:
            raise AgentError("OpenAI SDK is required: python -m pip install -U openai") from error
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=1)
        cases_dir = self.state.layout.cases_dir
        records = generate_api_test_cases.generate_cases(
            markdown_files=markdown_files,
            output_dir=cases_dir,
            client=client,
            model=model,
            include_images=include_images,
            retries=3,
            max_tokens=None,
            repair_attempts=2,
            skip_existing=skip_existing,
        )
        manifest = cases_dir / "generation_manifest.json"
        generated = sum(item.get("status") == "generated" for item in records)
        failed = [item for item in records if item.get("status") == "failed"]
        self.state.cases_dir = cases_dir.resolve()
        self.state.case_manifest = manifest.resolve()
        if failed:
            return _json_result(
                False,
                "failed",
                generated=generated,
                failed=len(failed),
                manifest=str(self.state.case_manifest),
                errors=[{"source": item.get("source"), "error": item.get("error")} for item in failed],
            )
        return _json_result(
            True,
            "completed",
            generated=generated,
            manifest=str(self.state.case_manifest),
            test_cases_dir=str(self.state.cases_dir),
            excel_files=[item.get("excel") for item in records if item.get("excel")],
        )

    def run_api_test_cases(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """阶段 4：全量预检配置后执行 HTTP，并回写 JSON、Excel 和 HTML。"""
        _check_arguments(arguments, {"execute", "timeout_ms"})
        if self.state.test_results_dir:
            return _json_result(True, "already_completed", results_dir=str(self.state.test_results_dir))
        if not self.state.cases_dir:
            return _json_result(False, "blocked", error="generate_api_test_cases must complete first")
        execute = _bool(arguments, "execute", False)
        timeout_ms = _optional_positive_int(arguments, "timeout_ms")
        paths = run_api_test_cases.discover_specs(self.state.cases_dir, "*_cases.json")
        valid: list[tuple[Path, dict[str, Any]]] = []
        invalid: list[dict[str, str]] = []
        for path in paths:
            try:
                valid.append((path, run_api_test_cases.load_spec(path)))
            except Exception as error:
                invalid.append({"source": str(path), "error": f"{type(error).__name__}: {error}"})
        if invalid:
            return _json_result(False, "failed", valid=len(valid), invalid=invalid)
        if not self.state.execute_requests:
            results_dir = self.state.layout.results_dir
            self.state.test_results_dir = results_dir.resolve()
            atomic_write_json(
                results_dir / "run_manifest.json",
                {
                    "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0},
                    "validation": {"valid_specs": len(valid), "live_execution": False},
                    "file_errors": [],
                    "reports": [],
                    "report": None,
                },
            )
            return _json_result(
                True,
                "validated_not_executed",
                valid=len(valid),
                results_dir=str(self.state.test_results_dir),
                message="Live HTTP execution is disabled by the task configuration.",
            )
        if not execute:
            return _json_result(True, "validated_not_executed", valid=len(valid), message="execute=false; no HTTP request was sent")
        if not self.state.base_url:
            return _json_result(
                False,
                "blocked",
                valid=len(valid),
                needs_user_input=True,
                missing_env=["API_BASE_URL"],
                question="执行自动化测试前，请提供目标测试环境的 API Base URL。",
                error="API_BASE_URL is required before HTTP tests can start.",
            )
        environment = load_env_file(self.state.env_file)
        environment.update(os.environ)
        environment.update(self.state.runtime_env)
        environment["API_BASE_URL"] = self.state.base_url
        required_env = run_api_test_cases.required_environment_names([spec for _, spec in valid])
        missing_env = run_api_test_cases.missing_required_environment(
            [spec for _, spec in valid],
            environment,
            base_url=self.state.base_url,
        )
        if missing_env:
            return _json_result(
                False,
                "blocked",
                valid=len(valid),
                needs_user_input=True,
                required_env=required_env,
                missing_env=missing_env,
                question="执行自动化测试前还需要以下信息：" + ", ".join(missing_env) + "。请用户补充后再执行。",
                error="Required runtime configuration is incomplete; no HTTP request was sent.",
            )
        results_dir = self.state.layout.results_dir
        reports: list[dict[str, Any]] = []
        file_errors: list[dict[str, str]] = []
        excel_workbooks: list[str] = []
        for path, spec in valid:
            try:
                report = run_api_test_cases.run_spec(
                    spec,
                    self.state.base_url,
                    environment,
                    timeout_ms=timeout_ms,
                )
                report["source"] = str(path)
                result_path = results_dir / f"{path.stem}_results.json"
                atomic_write_json(result_path, report)
                reports.append(report)
                if self.state.interface_markdown_dir:
                    try:
                        workbook_path = update_case_workbook_results(
                            path,
                            result_path,
                            self.state.interface_markdown_dir,
                        )
                        excel_workbooks.append(str(workbook_path))
                    except Exception as error:
                        file_errors.append(
                            {
                                "source": f"excel:{path.name}",
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
            except Exception as error:
                file_errors.append({"source": str(path), "error": f"{type(error).__name__}: {error}"})
        report_path = None
        if reports:
            try:
                report_path = write_html_report(results_dir)
            except Exception as error:
                file_errors.append({"source": "visualization", "error": f"{type(error).__name__}: {error}"})
        totals = {
            key: sum(report["summary"][key] for report in reports)
            for key in ("total", "passed", "failed", "skipped", "error")
        }
        atomic_write_json(
            results_dir / "run_manifest.json",
            {
                "summary": totals,
                "file_errors": file_errors,
                "reports": [report["source"] for report in reports],
                "excel_workbooks": excel_workbooks,
                "report": str(report_path) if report_path else None,
            },
        )
        self.state.test_results_dir = results_dir.resolve()
        return _json_result(
            True,
            "completed" if not file_errors else "completed_with_file_errors",
            summary=totals,
            has_failures=bool(totals["failed"] or totals["error"]),
            report=str(report_path) if report_path else None,
            results_dir=str(self.state.test_results_dir),
            excel_workbooks=excel_workbooks,
            file_errors=file_errors,
        )


def _assistant_message(message: Any) -> dict[str, Any]:
    """把 OpenAI SDK 消息对象转换为下一轮调用可复用的普通字典。"""
    payload: dict[str, Any] = {"role": "assistant", "content": message.content}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = [
            call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
            for call in tool_calls
        ]
    return payload


def _tool_call_parts(call: Any) -> tuple[str, str, str]:
    """兼容 SDK 对象和字典两种 tool_call 表示，提取调用三元组。"""
    if isinstance(call, Mapping):
        function = call.get("function", {})
        return str(call.get("id", "")), str(function.get("name", "")), str(function.get("arguments", "{}"))
    function = call.function
    return str(call.id), str(function.name), str(function.arguments)


def _message_content(value: Any) -> str:
    """从字符串或 OpenAI 内容块数组中提取最终可展示文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


class AgentToolExecutor(Protocol):
    """定义 Agent 与任意 CLI/Web 工具适配器之间的最小执行协议。"""

    def execute(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """执行一个白名单工具并返回可 JSON 序列化的结构化结果。"""
        ...


@dataclass(frozen=True)
class AgentTurnResult:
    """描述一轮 Agent 对话的最终文本与工具调用统计。"""

    content: str
    steps: int
    tool_calls: int
    blocked_for_input: bool = False


class ApiTestAgent:
    """CLI 与 Web 共用的 DeepSeek 对话及 OpenAI tool-calling 核心。"""

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        system_prompt: str = CHAT_SYSTEM_PROMPT,
        tools: Sequence[dict[str, Any]] = CHAT_TOOLS,
        max_steps: int = 6,
    ) -> None:
        """绑定模型客户端、行为策略、工具 schema 和单轮最大模型步数。"""
        if max_steps < 1:
            raise AgentError("max_steps must be positive")
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = list(tools)
        self.max_steps = max_steps

    @classmethod
    def from_env(
        cls,
        env_file: str | Path,
        *,
        system_prompt: str = CHAT_SYSTEM_PROMPT,
        tools: Sequence[dict[str, Any]] = CHAT_TOOLS,
        max_steps: int = 6,
    ) -> "ApiTestAgent":
        """从 .env/进程环境创建可直接用于 Web 或 CLI 的 Agent。"""
        client, model = build_client(Path(env_file).expanduser().resolve())
        return cls(client, model, system_prompt=system_prompt, tools=tools, max_steps=max_steps)

    def chat(
        self,
        *,
        history: Sequence[Mapping[str, Any]],
        message: str,
        tool_executor: AgentToolExecutor,
        context: str | None = None,
    ) -> AgentTurnResult:
        """处理一轮普通聊天或工具调用，并在需要用户配置时强制停止继续执行。"""
        user_message = message.strip()
        if not user_message:
            raise AgentError("Chat message must not be empty")
        if context:
            user_message += f"\n\n当前任务上下文：{context.strip()}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        tool_call_count = 0
        blocked_for_input = False
        blocked_question = ""
        for step in range(1, self.max_steps + 1):
            options: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "tools": self.tools,
                "tool_choice": "none" if blocked_for_input else "auto",
                "stream": False,
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            }
            response = self.client.chat.completions.create(**options)
            if not getattr(response, "choices", None):
                raise AgentError("DeepSeek returned no completion choices")
            model_message = response.choices[0].message
            content = _message_content(getattr(model_message, "content", None))
            tool_calls = list(getattr(model_message, "tool_calls", None) or [])
            messages.append(_assistant_message(model_message))

            if blocked_for_input:
                return AgentTurnResult(
                    content=content or blocked_question or "继续执行前需要用户补充测试环境信息。",
                    steps=step,
                    tool_calls=tool_call_count,
                    blocked_for_input=True,
                )
            if not tool_calls:
                if not content:
                    raise AgentError("DeepSeek returned neither content nor tool calls")
                return AgentTurnResult(content=content, steps=step, tool_calls=tool_call_count)

            for call in tool_calls:
                call_id, name, raw_arguments = _tool_call_parts(call)
                if blocked_for_input:
                    result = _json_result(
                        False,
                        "blocked",
                        error="前一个工具正在等待用户补充信息，本次工具未执行。",
                    )
                else:
                    try:
                        arguments = json.loads(raw_arguments or "{}")
                        if not isinstance(arguments, dict):
                            raise AgentError("Tool arguments must be a JSON object")
                        result = tool_executor.execute(name, arguments)
                        if not isinstance(result, dict):
                            raise AgentError("Tool result must be a JSON object")
                    except Exception as error:
                        result = _json_result(False, "failed", error=f"{type(error).__name__}: {error}")
                tool_call_count += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                if result.get("needs_user_input") or (
                    result.get("status") == "blocked" and result.get("missing_env")
                ):
                    blocked_for_input = True
                    blocked_question = str(result.get("question") or result.get("error") or "")

        raise AgentError(f"Agent exceeded the maximum of {self.max_steps} model turns")


def run_agent_loop(
    client: Any,
    model: str,
    host: ApiTestToolHost,
    *,
    max_steps: int,
    user_prompt: str | None = None,
) -> str:
    """通过统一 ApiTestAgent 运行命令行完整流水线策略。"""
    if host.state.pdf_path is None and not user_prompt:
        raise AgentError("A PDF location is required; pass --prompt, --pdf, or standard input")
    if user_prompt:
        content = user_prompt.strip()
        if host.state.pdf_path:
            content += f"\n\nActive PDF path already provided: {host.state.pdf_path}"
    else:
        content = (
            "Process the active PDF through the complete API-test pipeline. "
            f"PDF: {host.state.pdf_path}. "
            "Start by calling parse_api_document."
        )
    content += f"\nRun directory: {host.state.run_dir}"
    if host.state.pdf_path is None:
        content += "\nResolve the PDF location from the message above before parsing."
    else:
        content += "\nStart by calling parse_api_document once the PDF path is active."
    agent = ApiTestAgent(
        client,
        model,
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        max_steps=max_steps,
    )
    return agent.chat(history=[], message=content, tool_executor=host).content


def build_state(args: argparse.Namespace) -> PipelineState:
    """根据命令行配置创建独立运行目录和初始流水线状态。"""
    pdf_path = None
    if args.pdf:
        candidate = Path(args.pdf).expanduser().resolve()
        if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
            raise AgentError(f"--pdf must be an existing PDF file: {candidate}")
        pdf_path = candidate
    env_file = Path(args.env_file).expanduser().resolve()
    values = load_env_file(env_file)
    values.update(os.environ)
    base_url = args.base_url or values.get("API_BASE_URL")
    safe_stem = (
        "".join(char if char.isalnum() or char in "-_" else "_" for char in pdf_path.stem).strip("_")
        if pdf_path is not None
        else "api_document"
    )
    output_root = _inside_project(Path(args.output_root))
    run_dir = _inside_project(Path(args.run_dir)) if args.run_dir else output_root / f"{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if run_dir.exists():
        raise AgentError(f"Run directory already exists; use a new --run-dir: {run_dir}")
    run_dir.mkdir(parents=True)
    mineru_path = Path(args.mineru).expanduser().resolve() if args.mineru else None
    return PipelineState(
        pdf_path,
        run_dir,
        env_file,
        mineru_path,
        base_url,
        bool(getattr(args, "execute_requests", True)),
    )


def build_client(env_file: Path) -> tuple[Any, str]:
    """从 .env/进程环境创建 DeepSeek 的 OpenAI 兼容客户端。"""
    values = load_env_file(env_file)
    values.update(os.environ)
    api_key = values.get("DEEPSEEK_API_KEY")
    base_url = values.get("DEEPSEEK_BASE_URL")
    model = values.get("DEEPSEEK_MODEL")
    missing = [name for name, value in (("DEEPSEEK_API_KEY", api_key), ("DEEPSEEK_BASE_URL", base_url), ("DEEPSEEK_MODEL", model)) if not value]
    if missing:
        raise AgentError("Missing DeepSeek configuration: " + ", ".join(missing))
    try:
        from openai import OpenAI
    except ImportError as error:
        raise AgentError("OpenAI SDK is required: python -m pip install -U openai") from error
    return OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=1), str(model)


def run_pdf_pipeline(
    pdf_path: str | Path,
    *,
    env_file: str | Path = PROJECT_ROOT / ".env",
    base_url: str | None = None,
    mineru_path: str | Path | None = None,
    output_root: str | Path = PROJECT_ROOT / "output" / "agent_runs",
    run_dir: str | Path | None = None,
    max_steps: int = 16,
    execute_requests: bool = True,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """以函数方式运行命令行完整流水线，不创建额外 Python 子进程。

    当前 Web 聊天按用户指令逐个调用工具，不再自动调用本入口；它仍适用于
    CLI、批处理或其他 Python 集成。``on_tool_result`` 可接收工具进度事件。
    """
    if max_steps < 4:
        raise AgentError("max_steps must be at least 4")
    args = argparse.Namespace(
        pdf=str(pdf_path),
        env_file=str(env_file),
        base_url=base_url,
        mineru=str(mineru_path) if mineru_path else None,
        output_root=str(output_root),
        run_dir=str(run_dir) if run_dir else None,
        execute_requests=execute_requests,
    )
    state = build_state(args)
    host = ApiTestToolHost(state, on_tool_result=on_tool_result)
    try:
        client, model = build_client(state.env_file)
        summary = run_agent_loop(client, model, host, max_steps=max_steps)
        trace = {
            "pdf": str(state.pdf_path) if state.pdf_path else None,
            "run_dir": str(state.run_dir),
            "model": model,
            "tool_history": state.tool_history,
            "summary": summary,
        }
        atomic_write_json(state.layout.agent_trace, trace)
        return {"state": state, "summary": summary, "trace": trace}
    except Exception as error:
        trace = {
            "pdf": str(state.pdf_path) if state.pdf_path else None,
            "run_dir": str(state.run_dir),
            "tool_history": state.tool_history,
            "error": f"{type(error).__name__}: {error}",
        }
        atomic_write_json(state.layout.agent_trace, trace)
        raise


def build_parser() -> argparse.ArgumentParser:
    """创建完整 Agent 流水线的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Use a DeepSeek tool-calling agent to turn one API PDF into API test results.")
    parser.add_argument("--pdf", default=None, help="Optional input API PDF; omit it and tell the agent the PDF location via --prompt.")
    parser.add_argument("--prompt", default=None, help="Natural-language request that describes the PDF location or directory, e.g. \"PDF 在 D:/docs/api.pdf，请执行接口测试\".")
    parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", help="DeepSeek and optional API runtime environment file")
    parser.add_argument("--base-url", default=None, help="Target API base URL; overrides API_BASE_URL in .env")
    parser.add_argument("--mineru", default=os.getenv("MINERU_PATH"), help="Path to mineru.exe, used only when no reusable parse result exists")
    parser.add_argument("--output-root", default=PROJECT_ROOT / "output" / "agent_runs", help="Directory holding timestamped agent runs")
    parser.add_argument("--run-dir", default=None, help="Optional exact new run directory under this project")
    parser.add_argument("--max-steps", type=int, default=16, help="Maximum DeepSeek agent turns")
    return parser


def main() -> int:
    """运行命令行 Agent，对用户指令进行多轮工具调用并保存执行轨迹。"""
    args = build_parser().parse_args()
    if args.max_steps < 4:
        raise AgentError("--max-steps must be at least 4")
    prompt = (args.prompt or "").strip()
    if not prompt and not args.pdf:
        if sys.stdin and sys.stdin.isatty():
            prompt = input("请告诉我 PDF 文件路径或存放目录: ").strip()
        else:
            prompt = sys.stdin.read().strip()
    if not prompt and not args.pdf:
        raise AgentError("请通过 --prompt 描述 PDF 文件位置，或使用 --pdf 直接指定 PDF")
    state = build_state(args)
    host = ApiTestToolHost(state)
    try:
        client, model = build_client(state.env_file)
        summary = run_agent_loop(client, model, host, max_steps=args.max_steps, user_prompt=prompt or None)
        atomic_write_json(
            state.layout.agent_trace,
            {
                "pdf": str(state.pdf_path) if state.pdf_path else None,
                "user_prompt": prompt or None,
                "run_dir": str(state.run_dir),
                "model": model,
                "tool_history": state.tool_history,
                "summary": summary,
            },
        )
        print(summary)
        print(f"\nRun artifacts: {state.run_dir}")
        return 0
    except Exception as error:
        atomic_write_json(
            state.layout.agent_trace,
            {
                "pdf": str(state.pdf_path) if state.pdf_path else None,
                "user_prompt": prompt or None,
                "run_dir": str(state.run_dir),
                "tool_history": state.tool_history,
                "error": f"{type(error).__name__}: {error}",
            },
        )
        print(f"ERROR: {type(error).__name__}: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
