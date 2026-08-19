"""Local REST server and conversation manager for the API test agent.

Run ``python api_test_web.py`` and open http://127.0.0.1:8000.  A conversation
can start without a PDF; an API document may be attached later and pipeline
tools run only when the user asks the chat Agent to use them.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from api_test_agent import (
    ApiTestAgent,
    ApiTestToolHost,
    AgentTurnResult,
    PIPELINE_TOOL_NAMES,
    PipelineLayout,
    PipelineState,
    PROJECT_ROOT,
)


WEB_ROOT = PROJECT_ROOT / "webapp"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
TASK_ID = re.compile(r"^[a-f0-9]{32}$")
STAGE_BY_TOOL = {
    "parse_api_document": "parse",
    "split_api_interfaces": "split",
    "generate_api_test_cases": "cases",
    "run_api_test_cases": "test",
}
STAGE_LABELS = {
    "parse": "PDF 解析",
    "split": "接口切分",
    "cases": "测试用例生成",
    "test": "自动化测试",
}
TOOL_LABELS = {
    "parse_api_document": "解析接口 PDF",
    "split_api_interfaces": "切分接口文档",
    "generate_api_test_cases": "生成边界测试用例",
    "run_api_test_cases": "执行自动化测试",
    "list_workspace_files": "读取任务文件树",
    "read_workspace_file": "读取工作区文件",
    "write_workspace_file": "写入工作区文件",
    "copy_workspace_file": "复制工作区文件",
    "move_workspace_file": "移动工作区文件",
    "configure_test_environment": "配置测试环境",
    "get_task_progress": "读取任务进度",
}
MAX_CHAT_MESSAGE_CHARS = 8000
MAX_ARTIFACT_CHARS = 2_000_000
RUNTIME_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
WORKSPACE_TEXT_SUFFIXES = {".md", ".json", ".txt", ".html", ".log", ".csv", ".yaml", ".yml"}


def now() -> str:
    """返回带本地时区、精确到秒的当前 ISO 时间。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_filename(value: str) -> str:
    """清理上传文件名中的路径和非法字符，并确保使用 PDF 后缀。"""
    candidate = Path(value).name.strip().replace("\x00", "")
    candidate = re.sub(r"[\\/:*?\"<>|]+", "_", candidate)
    if not candidate.lower().endswith(".pdf"):
        candidate += ".pdf"
    return candidate[:180] or "api_document.pdf"


def json_load(path: Path, fallback: Any) -> Any:
    """容错读取 JSON 文件，文件缺失或格式错误时返回指定默认值。"""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def read_text(path: Path) -> str:
    """优先按 UTF-8 读取文本，失败时兼容常见中文编码。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gb18030", errors="replace")


@dataclass
class Task:
    """一个持久化聊天会话；upload_path 为 None 时仍可进行普通对话。"""
    task_id: str
    filename: str
    upload_path: Path | None
    run_dir: Path
    base_url: str | None
    execute_requests: bool
    title: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    status: str = "queued"
    active_stage: str | None = None
    active_tool: str | None = None
    stage_status: dict[str, str] = field(default_factory=lambda: {stage: "pending" for stage in STAGE_LABELS})
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    summary: str | None = None
    error: str | None = None
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    runtime_env: dict[str, str] = field(default_factory=dict, repr=False)


class WebTaskToolExecutor:
    """把统一 Agent 的工具调用适配到一个具体 Web 任务和 TaskStore。"""

    def __init__(self, store: "TaskStore", task: Task) -> None:
        """绑定当前会话，使所有文件路径和流水线状态都落在同一个 run_dir。"""
        self.store = store
        self.task = task

    def execute(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """将 Agent 工具调用交给 Web 会话工具路由执行。"""
        return self.store.execute_agent_tool(self.task, name, dict(arguments))


class TaskStore:
    """业务核心：持有会话状态、磁盘产物、聊天历史和 Agent 工具调度。"""
    def __init__(
        self,
        *,
        output_root: Path,
        env_file: Path,
        mineru_path: Path | None,
        max_steps: int,
        max_upload_bytes: int,
    ) -> None:
        """初始化会话存储配置，并从输出目录恢复已有会话。"""
        self.output_root = output_root.resolve()
        self.env_file = env_file.resolve()
        self.mineru_path = mineru_path.resolve() if mineru_path else None
        self.max_upload_bytes = max_upload_bytes
        self.agent = ApiTestAgent.from_env(self.env_file, max_steps=max_steps)
        self.tasks: dict[str, Task] = {}
        self.chat_locks: dict[str, threading.Lock] = {}
        self.lock = threading.RLock()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._restore_tasks()

    @staticmethod
    def _validated_base_url(value: Any) -> str | None:
        """校验测试目标 Base URL，拒绝账号信息、查询串和片段。"""
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("Base URL 必须是字符串")
        candidate = value.strip().rstrip("/")
        parts = urlparse(candidate)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Base URL 必须是无账号、query 和 fragment 的合法 http/https 地址")
        return candidate

    def _persist_task_metadata(self, task: Task) -> None:
        """原子写入轻量元数据；大文件和聊天记录分别保存在 run 目录。"""
        metadata_path = task.run_dir.parent / "task_meta.json"
        temporary_path = metadata_path.with_suffix(".json.tmp")
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(
                {
                    "filename": task.filename,
                    "title": task.title,
                    "created_at": task.created_at,
                    "has_pdf": bool(task.upload_path and task.upload_path.is_file()),
                    "base_url": task.base_url,
                    "execute_requests": task.execute_requests,
                    "runtime_env": task.runtime_env,
                    "updated_at": task.updated_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(metadata_path)

    def _restore_tasks(self) -> None:
        """Restore both chat-only conversations and API-document tasks."""

        for task_dir in self.output_root.iterdir():
            if not task_dir.is_dir() or not TASK_ID.fullmatch(task_dir.name):
                continue
            run_dir = task_dir / "run"
            layout = PipelineLayout(run_dir)
            uploads = sorted((task_dir / "upload").glob("*.pdf")) if (task_dir / "upload").is_dir() else []
            metadata_path = task_dir / "task_meta.json"
            if not run_dir.is_dir() or (not uploads and not metadata_path.is_file()):
                continue
            parse_done = layout.parse_manifest.is_file()
            split_done = layout.split_manifest.is_file()
            cases_done = layout.case_manifest.is_file()
            results_done = layout.run_manifest.is_file()
            trace = json_load(layout.agent_trace, {})
            timestamp = datetime.fromtimestamp(task_dir.stat().st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
            metadata = json_load(metadata_path, {})
            if not isinstance(metadata, dict):
                metadata = {}
            restored_base_url = None
            restored_execute = False
            try:
                restored_base_url = self._validated_base_url(metadata.get("base_url"))
            except ValueError:
                restored_base_url = None
            restored_execute = bool(metadata.get("execute_requests", False))
            task = Task(
                task_dir.name,
                uploads[0].name if uploads else str(metadata.get("filename") or "新会话"),
                uploads[0] if uploads else None,
                run_dir,
                restored_base_url,
                restored_execute,
                created_at=str(metadata.get("created_at") or timestamp),
                updated_at=timestamp,
            )
            if isinstance(metadata.get("title"), str):
                task.title = metadata["title"].strip() or None
            stored_env = metadata.get("runtime_env", {})
            if isinstance(stored_env, dict):
                task.runtime_env = {
                    name: value
                    for name, value in stored_env.items()
                    if isinstance(name, str)
                    and RUNTIME_ENV_NAME.fullmatch(name)
                    and not name.startswith(("DEEPSEEK_", "OPENAI_"))
                    and isinstance(value, str)
                }
            task.status = "completed" if all((parse_done, split_done, cases_done, results_done)) else ("ready" if uploads else "idle")
            task.stage_status = {
                "parse": "completed" if parse_done else "pending",
                "split": "completed" if split_done else "pending",
                "cases": "completed" if cases_done else "pending",
                "test": "completed" if results_done else "pending",
            }
            if isinstance(trace, dict):
                task.summary = trace.get("summary") if isinstance(trace.get("summary"), str) else None
                history = trace.get("tool_history")
                task.tool_history = history if isinstance(history, list) else []
                if trace.get("error"):
                    task.status = "failed"
                    task.error = str(trace["error"])
            if results_done:
                task.summary = "任务已从磁盘产物恢复；测试结果、异常明细与静态报告可继续查看。"
            history = json_load(layout.chat_history, [])
            task.chat_history = [dict(entry) for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []
            self.tasks[task.task_id] = task
            self.chat_locks[task.task_id] = threading.Lock()

    def create_chat_task(self, title: str | None = None) -> Task:
        """创建不依赖 PDF 的空会话；发送第一条纯文字消息时由前端调用。"""
        if title is not None:
            title = title.strip()
            if not title or len(title) > 80 or any(ord(character) < 32 for character in title):
                raise ValueError("会话名称必须是 1 至 80 个非控制字符")
        task_id = uuid.uuid4().hex
        task_dir = self.output_root / task_id
        run_dir = task_dir / "run"
        run_dir.mkdir(parents=True)
        task = Task(task_id, "新会话", None, run_dir, None, False, title=title, status="idle")
        self._persist_task_metadata(task)
        with self.lock:
            self.tasks[task_id] = task
            self.chat_locks[task_id] = threading.Lock()
        return task

    def attach_pdf(
        self,
        task: Task,
        filename: str,
        content: bytes,
        base_url: str | None,
        execute_requests: bool,
    ) -> Task:
        """给空会话保存一份 PDF；这里只落盘，不自动运行任何流水线阶段。"""
        if not content:
            raise ValueError("上传内容为空")
        if len(content) > self.max_upload_bytes:
            raise ValueError(f"PDF 超过 {self.max_upload_bytes // 1024 // 1024} MB 限制")
        if not content.lstrip().startswith(b"%PDF"):
            raise ValueError("上传内容不是有效的 PDF 文件")
        if task.upload_path is not None and task.upload_path.is_file():
            raise ValueError("当前会话已经附加 PDF；请新建会话后上传另一份文档")
        upload_dir = task.run_dir.parent / "upload"
        upload_dir.mkdir(parents=True)
        upload_path = upload_dir / safe_filename(filename)
        upload_path.write_bytes(content)
        base_url = self._validated_base_url(base_url)
        with self.lock:
            task.filename = upload_path.name
            task.upload_path = upload_path
            task.base_url = base_url
            task.execute_requests = execute_requests
            task.status = "ready"
            task.error = None
            task.updated_at = now()
            if not task.title:
                task.title = upload_path.stem
            self._persist_task_metadata(task)
        return task

    def create_task(self, filename: str, content: bytes, base_url: str | None, execute_requests: bool) -> Task:
        """兼容旧上传接口：新建会话并立即附加一份 PDF。"""
        task = self.create_chat_task()
        try:
            return self.attach_pdf(task, filename, content, base_url, execute_requests)
        except Exception:
            with self.lock:
                self.tasks.pop(task.task_id, None)
                self.chat_locks.pop(task.task_id, None)
            shutil.rmtree(task.run_dir.parent, ignore_errors=True)
            raise

    def _record_tool_event(self, task_id: str, entry: dict[str, Any]) -> None:
        """接收工具开始/结束事件，实时更新任务阶段状态和历史。"""
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            event = entry.get("event")
            tool = str(entry.get("tool", ""))
            stage = STAGE_BY_TOOL.get(tool)
            if event == "started":
                task.active_tool = tool or None
                if stage:
                    task.active_stage = stage
                    task.stage_status[stage] = "running"
            elif event == "completed":
                task.tool_history.append(dict(entry))
                if stage:
                    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
                    if result.get("status") == "validated_not_executed":
                        task.stage_status[stage] = "pending"
                    elif result.get("ok"):
                        task.stage_status[stage] = "completed"
                    elif result.get("status") == "blocked":
                        task.stage_status[stage] = "blocked"
                    else:
                        task.stage_status[stage] = "failed"
                    task.active_stage = None
                task.active_tool = None
            task.updated_at = now()

    def get(self, task_id: str) -> Task | None:
        """按 ID 获取内存中的会话，不存在时返回 None。"""
        with self.lock:
            return self.tasks.get(task_id)

    def list(self) -> list[Task]:
        """按创建时间倒序返回全部已恢复或新建的会话。"""
        with self.lock:
            return sorted(self.tasks.values(), key=lambda task: task.created_at, reverse=True)

    def rename_task(self, task: Task, title: str) -> dict[str, Any]:
        """修改会话显示名称，并同步持久化到 task_meta.json。"""
        title = title.strip()
        if not title:
            raise ValueError("会话名称不能为空")
        if len(title) > 80:
            raise ValueError("会话名称不能超过 80 个字符")
        if any(ord(character) < 32 for character in title):
            raise ValueError("会话名称不能包含控制字符")
        with self.lock:
            task.title = title
            task.updated_at = now()
            self._persist_task_metadata(task)
            return self.task_payload(task)

    def delete_task(self, task: Task) -> dict[str, Any]:
        """校验会话空闲后删除其后端任务目录和内存索引。"""
        if task.status in {"queued", "running"}:
            raise RuntimeError("任务仍在运行，完成后才能删除")
        chat_lock = self.chat_locks.setdefault(task.task_id, threading.Lock())
        if not chat_lock.acquire(blocking=False):
            raise RuntimeError("当前任务仍有一条消息正在处理")
        try:
            task_dir = task.run_dir.parent.resolve()
            if task_dir.parent != self.output_root or task_dir.name != task.task_id:
                raise RuntimeError("任务目录校验失败，拒绝删除")
            with self.lock:
                current = self.tasks.get(task.task_id)
                if current is not task:
                    raise FileNotFoundError("任务不存在")
                shutil.rmtree(task_dir)
                self.tasks.pop(task.task_id, None)
                self.chat_locks.pop(task.task_id, None)
            return {"deleted": True, "task_id": task.task_id}
        finally:
            chat_lock.release()

    def _relative_artifact(self, task: Task, value: str | Path) -> str | None:
        """把任务内绝对路径转换为相对于 run 目录的安全前端路径。"""
        path = Path(value)
        if not path.is_absolute():
            path = task.run_dir / path
        try:
            relative = path.resolve().relative_to(task.run_dir.resolve())
        except (OSError, ValueError):
            return None
        return relative.as_posix()

    def _artifact_node(self, task: Task, name: str, kind: str, path: Path | None) -> dict[str, Any]:
        """为左侧文件树构造一个带可用状态、大小和摘要的文件节点。"""
        relative = self._relative_artifact(task, path) if path else None
        available = bool(relative and path and path.is_file())
        node: dict[str, Any] = {"name": name, "kind": kind, "path": relative, "available": available}
        if available and path:
            node["size"] = path.stat().st_size
            if path.suffix.lower() == ".json":
                data = json_load(path, {})
                if kind == "cases" and isinstance(data, dict):
                    node["count"] = len(data.get("test_cases", [])) if isinstance(data.get("test_cases"), list) else 0
                elif kind == "results" and isinstance(data, dict):
                    node["summary"] = data.get("summary") if isinstance(data.get("summary"), dict) else None
        return node

    def artifact_tree_payload(self, task: Task) -> dict[str, Any]:
        """根据接口、用例和结果 manifest 组装前端左侧任务文件树。"""
        layout = PipelineLayout(task.run_dir)
        split_manifest = json_load(layout.split_manifest, {})
        markdown_records = split_manifest.get("markdown_files") if isinstance(split_manifest, dict) else []
        if not isinstance(markdown_records, list):
            markdown_records = []
        if not markdown_records:
            markdown_records = [
                {"markdown": str(path), "api_name": path.stem, "api_id": None}
                for path in sorted(layout.interfaces_dir.glob("[0-9][0-9][0-9]_*.md"))
            ]

        case_manifest = json_load(layout.case_manifest, {})
        case_records = case_manifest.get("records") if isinstance(case_manifest, dict) else []
        case_by_source: dict[str, Path] = {}
        if isinstance(case_records, list):
            for record in case_records:
                if not isinstance(record, dict) or not record.get("cases"):
                    continue
                case_by_source[Path(str(record.get("source", ""))).stem.lower()] = Path(str(record["cases"]))
        case_by_prefix = {
            path.name[:3]: path
            for path in sorted(layout.cases_dir.glob("[0-9][0-9][0-9]_*_cases.json"))
        }

        folders: list[dict[str, Any]] = []
        for index, record in enumerate(markdown_records):
            if not isinstance(record, dict):
                continue
            markdown_path = Path(str(record.get("markdown", "")))
            if not markdown_path.is_absolute():
                markdown_path = layout.interfaces_dir / markdown_path.name
            prefix = markdown_path.name[:3] if len(markdown_path.name) >= 3 else f"{index + 1:03d}"
            case_path = case_by_source.get(markdown_path.stem.lower()) or case_by_prefix.get(prefix)
            excel_path = markdown_path.with_name(f"{markdown_path.stem}_测试用例.xlsx")
            result_path = layout.results_dir / f"{case_path.stem}_results.json" if case_path else None
            files = [
                self._artifact_node(task, "接口文档.md", "markdown", markdown_path),
                self._artifact_node(task, "测试用例.json", "cases", case_path),
                self._artifact_node(task, "测试用例.xlsx", "excel", excel_path),
                self._artifact_node(task, "测试结果.json", "results", result_path),
            ]
            folders.append(
                {
                    "id": markdown_path.stem or f"interface-{index + 1}",
                    "name": record.get("api_name") or markdown_path.stem or f"接口 {index + 1}",
                    "operation_id": record.get("api_id"),
                    "files": files,
                    "available": sum(bool(item["available"]) for item in files),
                    "total": len(files),
                }
            )

        return {
            "task_id": task.task_id,
            "root_name": Path(task.filename).stem,
            "folders": folders,
            "report": self._artifact_node(task, "完整测试报告.html", "report", layout.report),
            "workspace_root": str(task.run_dir),
        }

    def _resolve_artifact(self, task: Task, relative_path: str) -> Path:
        """解析可读取文本产物，并阻止越目录、超大文件和非法类型访问。"""
        if not relative_path or "\x00" in relative_path or Path(relative_path).is_absolute():
            raise ValueError("文件路径无效")
        path = (task.run_dir / relative_path).resolve()
        try:
            path.relative_to(task.run_dir.resolve())
        except ValueError:
            raise ValueError("文件路径超出当前任务工作区") from None
        if path.suffix.lower() not in WORKSPACE_TEXT_SUFFIXES:
            raise ValueError("该文件类型不允许读取")
        if not path.is_file():
            raise FileNotFoundError("文件尚未生成或不存在")
        if path.stat().st_size > MAX_ARTIFACT_CHARS * 4:
            raise ValueError("文件过大，无法在页面中直接读取")
        return path

    def _resolve_workspace_item(self, task: Task, relative_path: str) -> Path:
        """解析允许由本机默认应用打开的任务文件。"""
        if not relative_path or "\x00" in relative_path or Path(relative_path).is_absolute():
            raise ValueError("文件路径无效")
        path = (task.run_dir / relative_path).resolve()
        try:
            path.relative_to(task.run_dir.resolve())
        except ValueError:
            raise ValueError("文件路径超出当前任务工作区") from None
        allowed = {".md", ".json", ".xlsx", ".txt", ".html", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise FileNotFoundError("文件尚未生成、不存在或不允许打开")
        return path

    def open_workspace_item(self, task: Task, relative_path: str) -> dict[str, Any]:
        """通过 Windows 默认程序在本机打开左侧文件树中的文件。"""
        path = self._resolve_workspace_item(task, relative_path)
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise RuntimeError("当前操作系统不支持通过默认程序打开本地文件")
        startfile(str(path))
        return {"task_id": task.task_id, "status": "opened", "path": self._relative_artifact(task, path), "name": path.name}

    def _artifact_text(self, task: Task, relative_path: str) -> tuple[Path, str]:
        """读取工作区文本；JSON 会先格式化以提高 Agent 可读性。"""
        path = self._resolve_artifact(task, relative_path)
        if path.suffix.lower() == ".json":
            data = json_load(path, None)
            content = json.dumps(data, ensure_ascii=False, indent=2) if data is not None else read_text(path)
        else:
            content = read_text(path)
        return path, content

    def artifact_content_payload(self, task: Task, relative_path: str) -> dict[str, Any]:
        """构造文件内容 REST 响应，包含语言、大小和截断标记。"""
        path, content = self._artifact_text(task, relative_path)
        language = {
            ".md": "markdown", ".json": "json", ".html": "html", ".txt": "text",
            ".log": "text", ".csv": "csv", ".yaml": "yaml", ".yml": "yaml",
        }.get(path.suffix.lower(), "text")
        return {
            "task_id": task.task_id,
            "path": self._relative_artifact(task, path),
            "name": path.name,
            "language": language,
            "size": path.stat().st_size,
            "content": content[:MAX_ARTIFACT_CHARS],
            "truncated": len(content) > MAX_ARTIFACT_CHARS,
        }

    def progress_payload(self, task: Task) -> dict[str, Any]:
        """把内存状态和 manifest 产物合并成前端需要的四阶段进度。"""
        tree = self.artifact_tree_payload(task)
        interface_total = len(tree["folders"])
        case_done = sum(any(file["kind"] == "cases" and file["available"] for file in folder["files"]) for folder in tree["folders"])
        result_done = sum(any(file["kind"] == "results" and file["available"] for file in folder["files"]) for folder in tree["folders"])
        unit_total = max(interface_total, 1)
        raw = {
            "parse": (1 if task.stage_status["parse"] == "completed" else 0, 1, "份文档"),
            "split": (interface_total if task.stage_status["split"] == "completed" else 0, unit_total, "个接口"),
            "cases": (case_done, unit_total, "个接口"),
            "test": (result_done, unit_total, "个接口"),
        }
        stages: list[dict[str, Any]] = []
        fractions: list[float] = []
        for stage, label in STAGE_LABELS.items():
            done, total, unit = raw[stage]
            status = task.stage_status[stage]
            if status == "completed":
                done = total
            fraction = min(done / total, 1.0) if total else 0.0
            if status == "running" and fraction == 0:
                fraction = 0.05
            fractions.append(fraction)
            stages.append({"id": stage, "label": label, "status": status, "completed": done, "total": total, "unit": unit, "percent": round(fraction * 100)})
        current = task.active_stage
        if current is None and task.status not in {"idle", "ready", "completed", "failed", "blocked"}:
            current = next((item["id"] for item in stages if item["status"] != "completed"), None)
        current_item = next((item for item in stages if item["id"] == current), None)
        if task.status == "idle":
            headline, detail = "可以直接聊天", "尚未附加 PDF；你也可以稍后在当前会话上传"
        elif task.status == "ready":
            completed_count = sum(item["status"] == "completed" for item in stages)
            next_stage = next((item["label"] for item in stages if item["status"] != "completed"), None)
            if completed_count:
                headline = f"已完成 {completed_count}/{len(stages)} 个阶段"
                detail = f"等待下一步指令；建议继续：{next_stage}" if next_stage else "等待新的任务指令"
            else:
                headline, detail = "PDF 已附加，等待指令", "上传不会自动执行；请在聊天中指定需要运行的阶段"
        elif task.status == "completed":
            headline = "全部子任务已完成"
            detail = f"共处理 {interface_total} 个接口"
        elif current_item:
            headline = f"正在执行：{current_item['label']}"
            tool_label = TOOL_LABELS.get(task.active_tool or "", task.active_tool or "")
            prefix = f"当前工具：{tool_label}；" if tool_label else ""
            detail = f"{prefix}已完成 {current_item['completed']}/{current_item['total']} {current_item['unit']}"
        elif task.status == "blocked":
            headline, detail = "任务等待配置", task.error or "请检查 Base URL 或运行凭据"
        elif task.status == "failed":
            headline, detail = "任务执行失败", task.error or "请查看执行轨迹"
        else:
            headline, detail = "Agent 正在准备", "等待第一个工具开始执行"
        return {
            "overall_percent": round(sum(fractions) / len(fractions) * 100),
            "current_stage": current,
            "headline": headline,
            "detail": detail,
            "stages": stages,
        }

    def chat_history_payload(self, task: Task) -> dict[str, Any]:
        """返回当前会话持久化聊天记录的安全副本。"""
        with self.lock:
            return {"task_id": task.task_id, "messages": list(task.chat_history)}

    def _persist_chat(self, task: Task) -> None:
        """把当前会话的完整聊天记录写入 run/chat_history.json。"""
        task.run_dir.mkdir(parents=True, exist_ok=True)
        PipelineLayout(task.run_dir).chat_history.write_text(
            json.dumps(task.chat_history, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _persist_agent_trace(self, task: Task, turn: AgentTurnResult) -> None:
        """原子保存统一 Agent 的最后回复、工具统计和当前 Web 工具历史。"""
        path = PipelineLayout(task.run_dir).agent_trace
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "updated_at": task.updated_at,
                    "summary": turn.content,
                    "agent": {
                        "steps": turn.steps,
                        "tool_calls": turn.tool_calls,
                        "blocked_for_input": turn.blocked_for_input,
                    },
                    "tool_history": task.tool_history,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _resolve_workspace_path(task: Task, relative_path: str, *, must_exist: bool = False) -> Path:
        """将 Agent 文件工具路径限制在当前任务 run 目录中。"""
        if not isinstance(relative_path, str) or not relative_path.strip() or "\x00" in relative_path:
            raise ValueError("工作区路径不能为空")
        candidate = Path(relative_path.strip())
        if candidate.is_absolute():
            raise ValueError("工作区工具只接受相对于任务 run 目录的路径")
        path = (task.run_dir / candidate).resolve()
        try:
            path.relative_to(task.run_dir.resolve())
        except ValueError:
            raise ValueError("工作区路径不能越出当前任务 run 目录") from None
        if must_exist and not path.exists():
            raise FileNotFoundError("工作区文件不存在")
        return path

    def _write_workspace_file(self, task: Task, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行受限文本写入，并要求显式指定追加或覆盖行为。"""
        unexpected = sorted(set(arguments) - {"path", "content", "append", "overwrite"})
        if unexpected:
            raise ValueError("不支持的参数：" + ", ".join(unexpected))
        path = self._resolve_workspace_path(task, str(arguments.get("path", "")))
        if path.suffix.lower() not in WORKSPACE_TEXT_SUFFIXES:
            raise ValueError("只允许写入 Markdown、JSON、TXT、HTML、LOG、CSV、YAML 文本文件")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content 必须是字符串")
        if len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("单次写入内容不能超过 2 MB")
        append = arguments.get("append", False)
        overwrite = arguments.get("overwrite", False)
        if not isinstance(append, bool) or not isinstance(overwrite, bool):
            raise ValueError("append 和 overwrite 必须是 boolean")
        if path.exists() and not append and not overwrite:
            raise FileExistsError("目标文件已存在；只有用户明确要求时才能设置 overwrite=true")
        if path.exists() and not path.is_file():
            raise ValueError("目标路径不是文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return {
            "ok": True,
            "status": "completed",
            "path": path.relative_to(task.run_dir).as_posix(),
            "bytes_written": len(content.encode("utf-8")),
        }

    def _transfer_workspace_file(self, task: Task, arguments: dict[str, Any], *, move: bool) -> dict[str, Any]:
        """在任务工作区内复制或移动单个文件，且不覆盖已有目标。"""
        unexpected = sorted(set(arguments) - {"source", "destination"})
        if unexpected:
            raise ValueError("不支持的参数：" + ", ".join(unexpected))
        source = self._resolve_workspace_path(task, str(arguments.get("source", "")), must_exist=True)
        destination = self._resolve_workspace_path(task, str(arguments.get("destination", "")))
        if not source.is_file():
            raise ValueError("当前工具只支持单个文件，不支持目录")
        if destination.exists():
            raise FileExistsError("目标文件已存在，不允许覆盖")
        if source == destination:
            raise ValueError("源文件和目标文件不能相同")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(source), str(destination))
        else:
            shutil.copy2(source, destination)
        return {
            "ok": True,
            "status": "completed",
            "operation": "move" if move else "copy",
            "source": source.relative_to(task.run_dir).as_posix(),
            "destination": destination.relative_to(task.run_dir).as_posix(),
            "size": destination.stat().st_size,
        }

    def _configure_test_environment(self, task: Task, arguments: dict[str, Any]) -> dict[str, Any]:
        """保存 Base URL 与运行时变量，供后续自动化测试原样注入。"""
        unexpected = sorted(set(arguments) - {"base_url", "execute_requests", "environment"})
        if unexpected:
            raise ValueError("不支持的参数：" + ", ".join(unexpected))
        base_url = self._validated_base_url(arguments.get("base_url"))
        if base_url is None:
            raise ValueError("执行测试前必须提供 Base URL")
        execute_requests = arguments.get("execute_requests", True)
        if not isinstance(execute_requests, bool):
            raise ValueError("execute_requests 必须是 boolean")
        environment = arguments.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError("environment 必须是 object")
        normalized: dict[str, str] = {}
        for name, value in environment.items():
            if not isinstance(name, str) or not RUNTIME_ENV_NAME.fullmatch(name):
                raise ValueError(f"环境变量名称无效：{name}")
            if name.startswith(("DEEPSEEK_", "OPENAI_")):
                raise ValueError("测试运行时变量不能覆盖模型配置")
            if name == "API_BASE_URL":
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"环境变量 {name} 必须是非空字符串")
            if len(value) > 65536:
                raise ValueError(f"环境变量 {name} 过长")
            normalized[name] = value
        with self.lock:
            task.base_url = base_url
            task.execute_requests = execute_requests
            task.runtime_env.update(normalized)
            if task.status == "blocked":
                task.status = "ready" if task.upload_path and task.upload_path.is_file() else "idle"
            if task.stage_status.get("test") == "blocked":
                task.stage_status["test"] = "pending"
            task.error = None
            task.updated_at = now()
            self._persist_task_metadata(task)
        return {
            "ok": True,
            "status": "configured",
            "base_url": base_url,
            "execute_requests": execute_requests,
            "environment_names": sorted(normalized),
            "environment": dict(normalized),
            "message": "运行时变量已原样保存到任务元数据，并注入后续测试。",
        }

    def _pipeline_state_for_task(self, task: Task) -> PipelineState:
        """从持久化会话重建工具层 PipelineState，连接 Web 层与工具层。"""
        if task.upload_path is None or not task.upload_path.is_file():
            raise ValueError("当前会话尚未附加 PDF，请先通过聊天框的“＋”上传接口文档")
        layout = PipelineLayout(task.run_dir)
        parse_artifacts = json_load(layout.parse_manifest, None) if layout.parse_manifest.is_file() else None
        return PipelineState(
            pdf_path=task.upload_path.resolve(),
            run_dir=task.run_dir.resolve(),
            env_file=self.env_file,
            mineru_path=self.mineru_path,
            base_url=task.base_url,
            execute_requests=task.execute_requests,
            runtime_env=dict(task.runtime_env),
            parse_artifacts=parse_artifacts if isinstance(parse_artifacts, dict) else None,
            split_manifest=layout.split_manifest.resolve() if layout.split_manifest.is_file() else None,
            interface_markdown_dir=layout.interfaces_dir.resolve() if layout.interfaces_dir.is_dir() else None,
            cases_dir=layout.cases_dir.resolve() if layout.cases_dir.is_dir() else None,
            case_manifest=layout.case_manifest.resolve() if layout.case_manifest.is_file() else None,
            # Keep this unset so an explicit chat request can rerun and overwrite result files.
            test_results_dir=None,
        )

    def _execute_pipeline_tool(self, task: Task, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行一个四阶段工具，并把工具状态同步回 Web Task。"""
        arguments = dict(arguments)
        if name == "parse_api_document" and "pdf_path" in arguments:
            raise ValueError("Web Agent 只能解析当前会话附加的 PDF，不允许覆盖 pdf_path")
        if name == "parse_api_document":
            arguments.setdefault("reuse_existing", False)
        if name == "run_api_test_cases":
            base_url = arguments.pop("base_url", None)
            environment = arguments.pop("environment", None)
            if base_url is not None or environment is not None:
                config_arguments: dict[str, Any] = {
                    "base_url": base_url or task.base_url,
                    "execute_requests": bool(arguments.get("execute", False)),
                }
                if environment is not None:
                    config_arguments["environment"] = environment
                self._configure_test_environment(task, config_arguments)
        state = self._pipeline_state_for_task(task)
        host = ApiTestToolHost(
            state,
            on_tool_result=lambda entry: self._record_tool_event(task.task_id, entry),
        )
        result = host.execute(name, arguments)
        with self.lock:
            task.base_url = state.base_url
            task.execute_requests = state.execute_requests
            task.runtime_env.update(state.runtime_env)
            task.error = None
            task.updated_at = now()
            if result.get("ok"):
                task.status = "completed" if all(value == "completed" for value in task.stage_status.values()) else "ready"
            elif result.get("status") == "blocked":
                task.status = "blocked"
            else:
                task.status = "failed"
            if name == "run_api_test_cases":
                task.summary = "聊天 Agent 已执行自动化测试：" + json.dumps(result, ensure_ascii=False)
            self._persist_task_metadata(task)
        return result

    def execute_agent_tool(self, task: Task, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行统一 Agent 发出的 Web 文件工具或公共流水线工具。"""
        if name == "list_workspace_files":
            return self.artifact_tree_payload(task)
        if name == "get_task_progress":
            return self.progress_payload(task)
        if name == "read_workspace_file":
            relative_path = str(arguments.get("path", ""))
            offset = max(int(arguments.get("offset", 0)), 0)
            max_chars = min(max(int(arguments.get("max_chars", 20000)), 1000), 30000)
            path, content = self._artifact_text(task, relative_path)
            chunk = content[offset : offset + max_chars]
            next_offset = offset + len(chunk)
            return {
                "path": self._relative_artifact(task, path),
                "offset": offset,
                "total_chars": len(content),
                "next_offset": next_offset if next_offset < len(content) else None,
                "content": chunk,
            }
        if name == "write_workspace_file":
            return self._write_workspace_file(task, arguments)
        if name == "copy_workspace_file":
            return self._transfer_workspace_file(task, arguments, move=False)
        if name == "move_workspace_file":
            return self._transfer_workspace_file(task, arguments, move=True)
        if name == "configure_test_environment":
            return self._configure_test_environment(task, arguments)
        if name in PIPELINE_TOOL_NAMES:
            if task.upload_path is None or not task.upload_path.is_file():
                return {
                    "ok": False,
                    "status": "blocked",
                    "needs_user_input": True,
                    "missing_attachment": ["PDF"],
                    "question": "当前会话还没有接口 PDF。请先通过聊天框的“＋”附加 PDF，再指定需要执行的阶段。",
                    "error": "A PDF attachment is required for pipeline tools.",
                }
            if task.status in {"queued", "running"} and task.active_stage is not None:
                return {"ok": False, "status": "blocked", "error": "当前任务已有流水线阶段正在执行，请等待完成"}
            with self.lock:
                task.status = "running"
                task.error = None
                task.updated_at = now()
            try:
                return self._execute_pipeline_tool(task, name, arguments)
            except Exception as error:
                with self.lock:
                    task.status = "failed"
                    task.active_stage = None
                    task.active_tool = None
                    task.error = f"{type(error).__name__}: {error}"
                    task.updated_at = now()
                    self._persist_task_metadata(task)
                return {"ok": False, "status": "failed", "error": task.error}
        return {"ok": False, "status": "failed", "error": f"未知工具：{name}"}

    def chat(self, task: Task, message: str, selected_path: str | None) -> dict[str, Any]:
        """单会话聊天事务：加锁、调用模型、成功后成对持久化 user/assistant。"""
        message = message.strip()
        if not message:
            raise ValueError("消息不能为空")
        if len(message) > MAX_CHAT_MESSAGE_CHARS:
            raise ValueError(f"消息不能超过 {MAX_CHAT_MESSAGE_CHARS} 个字符")
        if selected_path:
            self._resolve_workspace_item(task, selected_path)
        chat_lock = self.chat_locks.setdefault(task.task_id, threading.Lock())
        if not chat_lock.acquire(blocking=False):
            raise RuntimeError("当前任务已有一条消息正在处理")
        try:
            try:
                history = [
                    {"role": item["role"], "content": item["content"]}
                    for item in task.chat_history[-12:]
                    if item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), str)
                ]
                context = f"用户在左侧选中的文件：{selected_path}" if selected_path else None
                turn = self.agent.chat(
                    history=history,
                    message=message,
                    tool_executor=WebTaskToolExecutor(self, task),
                    context=context,
                )
            except Exception as error:
                raise RuntimeError(f"聊天 Agent 调用失败：{type(error).__name__}: {error}") from error
            user_entry = {
                "id": uuid.uuid4().hex,
                "role": "user",
                "content": message,
                "created_at": now(),
                "selected_path": selected_path,
            }
            assistant_entry = {
                "id": uuid.uuid4().hex,
                "role": "assistant",
                "content": turn.content,
                "created_at": now(),
                "agent": {
                    "steps": turn.steps,
                    "tool_calls": turn.tool_calls,
                    "blocked_for_input": turn.blocked_for_input,
                },
            }
            with self.lock:
                task.chat_history.extend([user_entry, assistant_entry])
                task.summary = turn.content
                task.updated_at = now()
                self._persist_chat(task)
                self._persist_agent_trace(task, turn)
                self._persist_task_metadata(task)
            return {"task_id": task.task_id, "message": assistant_entry}
        finally:
            chat_lock.release()

    def task_payload(self, task: Task) -> dict[str, Any]:
        """将内部 Task 状态转换为前端会话列表和详情所需的 JSON。"""
        with self.lock:
            return {
                "id": task.task_id,
                "filename": task.filename,
                "title": task.title or Path(task.filename).stem,
                "has_pdf": bool(task.upload_path and task.upload_path.is_file()),
                "status": task.status,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "active_stage": task.active_stage,
                "active_tool": task.active_tool,
                "progress": self.progress_payload(task),
                "stages": [
                    {"id": stage, "label": STAGE_LABELS[stage], "status": task.stage_status[stage]}
                    for stage in STAGE_LABELS
                ],
                "summary": task.summary,
                "error": task.error,
                "http_execution_configured": bool(task.base_url and task.execute_requests),
                "http_execution_requested": task.execute_requests,
                "tool_history": list(task.tool_history),
                "links": {
                    "interfaces": f"/api/tasks/{task.task_id}/interfaces",
                    "test_cases": f"/api/tasks/{task.task_id}/test-cases",
                    "results": f"/api/tasks/{task.task_id}/results",
                    "report": f"/api/tasks/{task.task_id}/report",
                    "files": f"/api/tasks/{task.task_id}/files",
                    "chat": f"/api/tasks/{task.task_id}/chat",
                },
            }

    def interfaces_payload(self, task: Task) -> dict[str, Any]:
        """读取切分 manifest 并返回每个接口的名称和 Markdown。"""
        manifest = json_load(PipelineLayout(task.run_dir).split_manifest, {})
        records = manifest.get("markdown_files") if isinstance(manifest, dict) else []
        interfaces: list[dict[str, Any]] = []
        for index, record in enumerate(records if isinstance(records, list) else []):
            if not isinstance(record, dict):
                continue
            path = Path(str(record.get("markdown", "")))
            if path.is_file():
                interfaces.append(
                    {
                        "index": index,
                        "name": record.get("api_name") or path.stem,
                        "operation_id": record.get("api_id"),
                        "markdown": read_text(path),
                    }
                )
        return {"task_id": task.task_id, "count": len(interfaces), "interfaces": interfaces}

    def case_payload(self, task: Task) -> dict[str, Any]:
        """读取生成 manifest 并汇总各接口的声明式测试用例。"""
        manifest = json_load(PipelineLayout(task.run_dir).case_manifest, {})
        records = manifest.get("records") if isinstance(manifest, dict) else []
        cases: list[dict[str, Any]] = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or record.get("status") not in {"generated", "skipped"}:
                continue
            path = Path(str(record.get("cases", "")))
            data = json_load(path, None)
            if not isinstance(data, dict):
                continue
            cases.append(
                {
                    "source": record.get("source"),
                    "case_count": record.get("case_count") or len(data.get("test_cases", [])),
                    "interface": data.get("interface"),
                    "data": data,
                }
            )
        return {"task_id": task.task_id, "count": len(cases), "cases": cases}

    def results_payload(self, task: Task) -> dict[str, Any]:
        """汇总全部测试结果，计算通过率、执行率、异常数和响应耗时。"""
        layout = PipelineLayout(task.run_dir)
        results_dir = layout.results_dir
        manifest = json_load(layout.run_manifest, {})
        reports: list[dict[str, Any]] = []
        for path in sorted(results_dir.glob("*_results.json")) if results_dir.is_dir() else []:
            data = json_load(path, None)
            if isinstance(data, dict):
                reports.append(data)
        flat = [item for report in reports for item in report.get("results", []) if isinstance(item, dict)]
        counts = {key: sum(1 for item in flat if item.get("status") == key) for key in ("passed", "failed", "error", "skipped")}
        total = len(flat)
        executable = total - counts["skipped"]
        durations = [float(item["elapsed_ms"]) for item in flat if isinstance(item.get("elapsed_ms"), (int, float))]
        anomalies = [item for item in flat if item.get("status") in {"failed", "error"}]
        metrics = {
            "total": total,
            **counts,
            "pass_rate": round((counts["passed"] / executable * 100), 1) if executable else None,
            "execution_rate": round((executable / total * 100), 1) if total else 0,
            "abnormal_count": len(anomalies),
            "avg_response_ms": round(sum(durations) / len(durations), 2) if durations else None,
            "max_response_ms": max(durations) if durations else None,
            "validated_specs": (
                manifest.get("validation", {}).get("valid_specs")
                if isinstance(manifest, dict) and isinstance(manifest.get("validation"), dict)
                else None
            ),
            "live_execution": (
                manifest.get("validation", {}).get("live_execution")
                if isinstance(manifest, dict) and isinstance(manifest.get("validation"), dict)
                else bool(total)
            ),
        }
        return {
            "task_id": task.task_id,
            "status": task.status,
            "metrics": metrics,
            "manifest": manifest if isinstance(manifest, dict) else {},
            "reports": reports,
            "anomalies": anomalies,
            "report_available": layout.report.is_file(),
        }


class ApiHandler(BaseHTTPRequestHandler):
    """很薄的 REST 适配层：负责 HTTP 校验，把业务委托给 TaskStore。"""
    store: TaskStore

    server_version = "ApiTestAgentWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        """以统一前缀将 HTTP 访问日志输出到后端终端。"""
        print(f"[web] {self.address_string()} {format % args}")

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        """发送禁用缓存的 UTF-8 JSON HTTP 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        """发送静态文件或任务报告，不存在时返回 JSON 404。"""
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "文件不存在"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _task_or_404(self, task_id: str) -> Task | None:
        """校验并查询任务 ID，失败时直接写出 404 响应。"""
        if not TASK_ID.fullmatch(task_id):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
            return None
        task = self.store.get(task_id)
        if task is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
            return None
        return task

    def _read_json_body(self, max_bytes: int = 64 * 1024) -> dict[str, Any] | None:
        """校验 Content-Type/Length 并读取大小受限的 JSON object 请求体。"""
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Content-Length 无效"})
            return None
        if content_length <= 0 or content_length > max_bytes:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "JSON 请求体大小无效"})
            return None
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "请使用 application/json"})
            return None
        try:
            value = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON 请求体无效"})
            return None
        if not isinstance(value, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON 顶层必须是 object"})
            return None
        return value

    def _read_pdf_upload(self) -> tuple[str, bytes, str | None, bool] | None:
        """读取原始 PDF 上传体及请求头中的文件名和测试配置。"""
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Content-Length 无效"})
            return None
        if content_length <= 0 or content_length > self.store.max_upload_bytes:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "PDF 文件大小不在允许范围内"})
            return None
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/pdf", "application/octet-stream"}:
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "请以 application/pdf 上传文件"})
            return None
        filename = unquote(self.headers.get("X-Filename", "api_document.pdf"))
        base_url = self.headers.get("X-API-Base-URL", "").strip() or None
        execute_requests = self.headers.get("X-Execute-Tests", "true").strip().lower() not in {"0", "false", "no", "off"}
        return filename, self.rfile.read(content_length), base_url, execute_requests

    def do_GET(self) -> None:
        """提供静态页面、任务查询、文件树、结果与聊天历史。"""
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/":
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if route in {"/assets/app.js", "/assets/styles.css", "/assets/marked.umd.js"}:
            filename = route.rsplit("/", 1)[-1]
            content_type = "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
            self._send_file(WEB_ROOT / filename, content_type)
            return
        if route == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "api-test-agent-web"})
            return
        if route == "/api/tasks":
            self._send_json(HTTPStatus.OK, {"tasks": [self.store.task_payload(task) for task in self.store.list()]})
            return
        segments = [segment for segment in route.split("/") if segment]
        if len(segments) >= 3 and segments[:2] == ["api", "tasks"]:
            task = self._task_or_404(segments[2])
            if task is None:
                return
            if len(segments) == 3:
                self._send_json(HTTPStatus.OK, self.store.task_payload(task))
                return
            if len(segments) == 4 and segments[3] == "interfaces":
                self._send_json(HTTPStatus.OK, self.store.interfaces_payload(task))
                return
            if len(segments) == 4 and segments[3] == "test-cases":
                self._send_json(HTTPStatus.OK, self.store.case_payload(task))
                return
            if len(segments) == 4 and segments[3] == "results":
                self._send_json(HTTPStatus.OK, self.store.results_payload(task))
                return
            if len(segments) == 4 and segments[3] == "report":
                self._send_file(PipelineLayout(task.run_dir).report, "text/html; charset=utf-8")
                return
            if len(segments) == 4 and segments[3] == "files":
                self._send_json(HTTPStatus.OK, self.store.artifact_tree_payload(task))
                return
            if len(segments) == 5 and segments[3:] == ["files", "content"]:
                relative_path = parse_qs(parsed.query).get("path", [""])[0]
                try:
                    payload = self.store.artifact_content_payload(task, relative_path)
                except FileNotFoundError as error:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                    return
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if len(segments) == 4 and segments[3] == "chat":
                self._send_json(HTTPStatus.OK, self.store.chat_history_payload(task))
                return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def do_PATCH(self) -> None:
        """处理会话重命名请求并同步后端元数据。"""
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 3 or segments[:2] != ["api", "tasks"]:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        task = self._task_or_404(segments[2])
        if task is None:
            return
        payload = self._read_json_body()
        if payload is None:
            return
        title = payload.get("title")
        if not isinstance(title, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "title 必须是字符串"})
            return
        try:
            result = self.store.rename_task(task, title)
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._send_json(HTTPStatus.OK, result)

    def do_DELETE(self) -> None:
        """处理会话删除请求并删除对应后端任务目录。"""
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 3 or segments[:2] != ["api", "tasks"]:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        task = self._task_or_404(segments[2])
        if task is None:
            return
        try:
            result = self.store.delete_task(task)
        except FileNotFoundError as error:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            return
        except RuntimeError as error:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self._send_json(HTTPStatus.OK, result)

    def do_POST(self) -> None:
        """处理会话创建、PDF 附加、聊天消息和本机文件打开。"""
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) == 5 and segments[:2] == ["api", "tasks"] and segments[3:] == ["files", "open"]:
            task = self._task_or_404(segments[2])
            if task is None:
                return
            payload = self._read_json_body()
            if payload is None:
                return
            relative_path = payload.get("path")
            if not isinstance(relative_path, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "path 必须是字符串"})
                return
            try:
                result = self.store.open_workspace_item(task, relative_path)
            except FileNotFoundError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except RuntimeError as error:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if len(segments) == 4 and segments[:2] == ["api", "tasks"] and segments[3] == "pdf":
            task = self._task_or_404(segments[2])
            if task is None:
                return
            upload = self._read_pdf_upload()
            if upload is None:
                return
            try:
                result = self.store.attach_pdf(task, *upload)
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, self.store.task_payload(result))
            return
        if len(segments) == 4 and segments[:2] == ["api", "tasks"] and segments[3] == "chat":
            task = self._task_or_404(segments[2])
            if task is None:
                return
            payload = self._read_json_body()
            if payload is None:
                return
            message = payload.get("message")
            selected_path = payload.get("selected_path")
            if not isinstance(message, str) or (selected_path is not None and not isinstance(selected_path, str)):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message 必须是字符串，selected_path 必须是字符串或 null"})
                return
            try:
                result = self.store.chat(task, message, selected_path)
            except FileNotFoundError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except RuntimeError as error:
                status = HTTPStatus.CONFLICT if "已有一条消息" in str(error) else HTTPStatus.BAD_GATEWAY
                self._send_json(status, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if parsed.path != "/api/tasks":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            payload = self._read_json_body()
            if payload is None:
                return
            title = payload.get("title")
            if title is not None and not isinstance(title, str):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "title 必须是字符串或 null"})
                return
            try:
                task = self.store.create_chat_task(title)
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(HTTPStatus.CREATED, self.store.task_payload(task))
            return
        upload = self._read_pdf_upload()
        if upload is None:
            return
        try:
            task = self.store.create_task(*upload)
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        payload = self.store.task_payload(task)
        self._send_json(HTTPStatus.CREATED, payload)


def build_parser() -> argparse.ArgumentParser:
    """创建本地 REST 服务的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Local REST backend and frontend for the API PDF test Agent")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host; default is loopback only")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", help="DeepSeek and runtime environment file")
    parser.add_argument("--mineru", default=None, help="Optional path to mineru.exe for PDFs without reusable artifacts")
    parser.add_argument("--output-root", default=PROJECT_ROOT / "output" / "agent_ui_runs", help="Task artifact directory")
    parser.add_argument("--max-steps", type=int, default=16, help="Maximum Agent model turns per task")
    parser.add_argument("--max-upload-mb", type=int, default=100, help="Maximum uploaded PDF size in MB")
    return parser


def main() -> int:
    """初始化任务存储并启动支持多线程请求的本地 HTTP 服务。"""
    args = build_parser().parse_args()
    if not WEB_ROOT.is_dir():
        raise SystemExit(f"Frontend directory does not exist: {WEB_ROOT}")
    if args.port < 1 or args.port > 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.max_steps < 4:
        raise SystemExit("--max-steps must be at least 4")
    if args.max_upload_mb < 1:
        raise SystemExit("--max-upload-mb must be positive")
    output_root = Path(args.output_root).expanduser().resolve()
    try:
        output_root.relative_to(PROJECT_ROOT)
    except ValueError:
        raise SystemExit("--output-root must be inside this project") from None
    ApiHandler.store = TaskStore(
        output_root=output_root,
        env_file=Path(args.env_file),
        mineru_path=Path(args.mineru) if args.mineru else None,
        max_steps=args.max_steps,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
    )
    server = ThreadingHTTPServer((args.host, args.port), ApiHandler)
    print(f"API Test Agent Web: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
