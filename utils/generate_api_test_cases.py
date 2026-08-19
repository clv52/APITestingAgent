"""Generate validated, declarative boundary test cases from interface Markdown."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from api_test_case_spec import (
    SCHEMA_VERSION,
    SpecError,
    atomic_write_json,
    load_env_file,
    safe_file_stem,
    validate_test_spec,
)
from export_test_cases_excel import export_case_workbook


PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?:\s+['\"][^)]*['\"])?\s*\)")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

SYSTEM_PROMPT = f"""
你是一名资深 API 测试工程师。仅根据提供的一个接口 Markdown，生成覆盖边界的单接口测试用例 JSON。

严格要求：
1. 每条 test_case 只描述一次 HTTP 请求和一次响应校验。禁止生成 Python、pytest、shell、SQL、JavaScript 或任何可执行代码。
2. 覆盖适用于该接口的正常值、必填缺失、null、空串、空白串、类型错误、枚举、长度上下界、超长、特殊字符、Unicode、URL 编码、鉴权、错误 Method、错误 Content-Type 等场景。
3. 文档未给出精确限制时不得虚构最大长度或确切错误码；用 assumption 记录假设，并避免过度断言。
4. 真实凭据必须使用合法 JSON object {{"$env": "NAME"}}，或 {{"$template": "Bearer ${{ACCESS_TOKEN}}"}}；必须在 required_env 中声明变量名。不得把构造器写成字符串，不得输出真实 token、密码、secret、授权码、ticket。
5. 参数缺失通过从 headers/query/body.fields/path_params 中省略该参数表示；null 使用 JSON null；空串使用 ""；空白串使用 "   "。
6. 超长字符串使用合法 JSON object {{"$repeat": {{"text": "a", "count": 1025}}}}，不要把构造器写成字符串，也不要在 JSON 中展开大字符串。
7. method/path 默认取 interface。需要测试错误 Method 或错误 Path 时，才在 request 覆盖它。path 必须始终是相对路径，不得输出完整 URL。
8. response JSON 路径使用 JSON Pointer，例如 /access_token、/data/user/id、/items/0/id。
9. 必须只输出一个 JSON object，不能使用 Markdown 代码围栏或解释文字。

顶层 schema_version 必须是 {SCHEMA_VERSION!r}。输出必须符合以下结构：
{json.dumps({
  "schema_version": SCHEMA_VERSION,
  "interface": {"name": "接口名称", "operation_id": "操作标识或 null", "method": "GET/POST/...", "path": "/relative/path"},
  "execution": {"timeout_ms": 10000, "follow_redirects": False},
  "required_env": ["API_BASE_URL"],
  "test_cases": [{
    "id": "唯一ID", "title": "用例标题", "category": "positive|required|length|type|enum|auth|protocol|encoding|security|other", "priority": "P0|P1|P2",
    "boundary": {"target": "参数或接口行为", "rule": "边界规则", "value": "可选"}, "assumption": None,
    "request": {
      "headers": [{"name": "Header", "value": "值"}], "query": [{"name": "参数", "value": "值"}], "path_params": {},
      "body": None
    },
    "expected": {
      "status_codes": [200], "headers": [],
      "json": [{"path": "/field", "op": "exists"}], "text": [], "max_response_ms": 3000
    }
  }]
}, ensure_ascii=False, indent=2)}

body 只允许：null；{{"type":"form","fields":[{{"name":"x","value":"y"}}]}}；{{"type":"json","value":{{}}}}；{{"type":"raw","content_type":"...","text":"..."}}。
expected.headers 的 op 只允许 exists/equals/contains/not_contains/matches。
expected.json 的 op 只允许 exists/not_exists/equals/not_equals/type/not_empty/contains/matches。
expected.text 的 op 只允许 equals/contains/not_contains/matches。
""".strip()


class GenerationError(RuntimeError):
    """Raised for local configuration or model-generation failures."""


def _merged_model_environment(env_path: Path) -> dict[str, str]:
    """合并模型配置文件与进程环境变量，进程中的配置优先。"""
    values = load_env_file(env_path)
    values.update(os.environ)
    return values


def _model_config(env_path: Path) -> tuple[str, str, str]:
    """读取并校验 DeepSeek/OpenAI 兼容接口所需的 key、base URL 和模型名。"""
    values = _merged_model_environment(env_path)
    api_key = values.get("DEEPSEEK_API_KEY") or values.get("OPENAI_API_KEY")
    base_url = values.get("DEEPSEEK_BASE_URL") or values.get("OPENAI_BASE_URL")
    model = values.get("DEEPSEEK_MODEL") or values.get("OPENAI_MODEL")
    missing = [
        name for name, value in (("DEEPSEEK_API_KEY", api_key), ("DEEPSEEK_BASE_URL", base_url), ("DEEPSEEK_MODEL", model)) if not value
    ]
    if missing:
        raise GenerationError("缺少模型配置：" + ", ".join(missing))
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GenerationError("DEEPSEEK_BASE_URL 必须是合法 http/https URL")
    return api_key, base_url, model


def discover_markdown(input_path: Path, pattern: str) -> list[Path]:
    """从单个文件或目录中发现需要生成用例的接口 Markdown。"""
    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        if input_path.suffix.lower() != ".md":
            raise GenerationError(f"输入文件必须是 Markdown：{input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise GenerationError(f"输入路径不存在：{input_path}")
    files = sorted(path.resolve() for path in input_path.glob(pattern) if path.is_file())
    if not files:
        raise GenerationError(f"没有找到匹配 {pattern!r} 的接口 Markdown")
    return files


def _is_within(path: Path, root: Path) -> bool:
    """判断解析后的路径是否仍位于指定根目录内。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_markdown_images(markdown_path: Path, text: str, max_count: int = 8, max_bytes: int = 20 * 1024 * 1024) -> tuple[list[Path], list[str]]:
    """按 Markdown 所在目录解析图片，并拒绝远程 URL 与越目录路径。"""
    root = markdown_path.parent.resolve()
    images: list[Path] = []
    warnings: list[str] = []
    total = 0
    seen: set[Path] = set()
    for match in IMAGE_LINK.finditer(text):
        raw = match.group("target").strip().strip("<>")
        parsed = urllib.parse.urlsplit(urllib.parse.unquote(raw))
        if parsed.scheme or raw.startswith("//"):
            warnings.append(f"跳过非本地图片：{raw}")
            continue
        path = (root / parsed.path).resolve()
        if not _is_within(path, root):
            warnings.append(f"跳过越出 Markdown 目录的图片：{raw}")
            continue
        if path in seen:
            continue
        seen.add(path)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            warnings.append(f"跳过不支持的图片类型：{raw}")
            continue
        if not path.is_file():
            warnings.append(f"图片不存在：{path}")
            continue
        if len(images) >= max_count or total + path.stat().st_size > max_bytes:
            warnings.append(f"图片数量或总大小超过限制，跳过：{path.name}")
            continue
        total += path.stat().st_size
        images.append(path)
    return images, warnings


def _data_url(path: Path) -> str:
    """把本地图片编码为模型多模态消息可使用的 data URL。"""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_messages(markdown_path: Path, markdown: str, images: list[Path]) -> list[dict[str, Any]]:
    """构造“一份接口文档对应一次生成请求”的模型消息。"""
    text = (
        f"接口文档文件名：{markdown_path.name}\n\n"
        "只针对下面这一个接口输出测试用例 JSON。\n"
        "===== INTERFACE MARKDOWN START =====\n"
        f"{markdown}\n"
        "===== INTERFACE MARKDOWN END ====="
    )
    if not images:
        user_content: str | list[dict[str, Any]] = text
    else:
        user_content = [{"type": "text", "text": text}]
        user_content.extend(
            {"type": "image_url", "image_url": {"url": _data_url(image), "detail": "high"}}
            for image in images
        )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}]


def _content_text(content: Any) -> str:
    """兼容字符串或内容块数组形式的模型响应，提取最终文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _parse_json(raw: str) -> dict[str, Any]:
    """从模型文本中提取并解析一个顶层 JSON object。"""
    stripped = raw.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            raise GenerationError("模型返回内容中不存在 JSON object") from None
        try:
            result, _ = json.JSONDecoder().raw_decode(stripped[start:])
        except json.JSONDecodeError as error:
            raise GenerationError(f"模型返回 JSON 解析失败：{error}") from error
    if not isinstance(result, dict):
        raise GenerationError("模型返回的顶层内容必须是 JSON object")
    return result


def call_model(client: Any, model: str, messages: list[dict[str, Any]], retries: int, max_tokens: int | None) -> str:
    """调用 DeepSeek OpenAI 兼容接口；这里只负责传输和空响应重试。"""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            options: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            }
            if max_tokens is not None:
                options["max_tokens"] = max_tokens
            response = client.chat.completions.create(**options)
            content = _content_text(response.choices[0].message.content)
            if content:
                return content
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            raise GenerationError(f"模型返回 content 为空，finish_reason={finish_reason!r}")
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(attempt * 2, 8))
    raise GenerationError(f"模型调用失败：{last_error}")


def request_valid_spec(client: Any, model: str, messages: list[dict[str, Any]], retries: int, max_tokens: int | None, repair_attempts: int) -> dict[str, Any]:
    """把不稳定的模型文本收敛为通过本地 schema 的确定性 JSON。"""
    raw = call_model(client, model, messages, retries, max_tokens)
    for repair in range(repair_attempts + 1):
        try:
            return validate_test_spec(_parse_json(raw))
        except (GenerationError, SpecError) as error:
            if repair >= repair_attempts:
                raise GenerationError(f"模型用例不符合 schema：{error}") from error
            # 修复轮次只允许修格式/字段，不能借机重新设计测试逻辑。
            repair_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "以下结果未通过本地 schema 校验。只修复格式或缺失字段，"
                        "不要生成代码；输出完整 JSON object。\n"
                        f"校验错误：{error}\n"
                        f"===== INVALID RESULT START =====\n{raw[:60000]}\n===== INVALID RESULT END ====="
                    ),
                },
            ]
            raw = call_model(client, model, repair_messages, retries, max_tokens)
    raise AssertionError("unreachable")


def generate_cases(
    markdown_files: list[Path],
    output_dir: Path,
    client: Any,
    model: str,
    *,
    include_images: bool,
    retries: int,
    max_tokens: int | None,
    repair_attempts: int,
    skip_existing: bool,
) -> list[dict[str, Any]]:
    """逐接口生成 JSON 和 Excel，并用 manifest 记录部分失败而不中断整批。"""
    output_dir = output_dir.expanduser().resolve()
    records: list[dict[str, Any]] = []
    for markdown_path in markdown_files:
        warnings: list[str] = []
        stem = safe_file_stem(markdown_path.stem)
        case_path = output_dir / f"{stem}_cases.json"
        excel_path = markdown_path.with_name(f"{markdown_path.stem}_测试用例.xlsx")
        if skip_existing and case_path.is_file():
            try:
                if not excel_path.is_file():
                    existing_spec = validate_test_spec(json.loads(case_path.read_text(encoding="utf-8-sig")))
                    export_case_workbook(case_path, excel_path)
                    records.append({
                        "source": str(markdown_path), "status": "generated", "cases": str(case_path),
                        "excel": str(excel_path), "case_count": len(existing_spec["test_cases"]),
                        "warnings": ["复用已有 JSON，并补生成 Excel 可视化文件"],
                    })
                else:
                    records.append({
                        "source": str(markdown_path), "status": "skipped", "cases": str(case_path),
                        "excel": str(excel_path), "warnings": warnings,
                    })
            except Exception as error:
                records.append({
                    "source": str(markdown_path), "status": "failed",
                    "error": f"{type(error).__name__}: {error}", "warnings": warnings,
                })
            continue
        try:
            markdown = markdown_path.read_text(encoding="utf-8-sig")
            images, image_warnings = resolve_markdown_images(markdown_path, markdown)
            warnings.extend(image_warnings)
            messages = build_messages(markdown_path, markdown, images if include_images else [])
            spec = request_valid_spec(client, model, messages, retries, max_tokens, repair_attempts)
            atomic_write_json(case_path, spec)
            export_case_workbook(case_path, excel_path)
            records.append({
                "source": str(markdown_path), "status": "generated", "cases": str(case_path),
                "excel": str(excel_path),
                "case_count": len(spec["test_cases"]), "images": [str(path) for path in images] if include_images else [], "warnings": warnings,
            })
        except Exception as error:
            records.append({"source": str(markdown_path), "status": "failed", "error": f"{type(error).__name__}: {error}", "warnings": warnings})
    atomic_write_json(output_dir / "generation_manifest.json", {
        "total": len(records),
        "generated": sum(item["status"] == "generated" for item in records),
        "skipped": sum(item["status"] == "skipped" for item in records),
        "failed": sum(item["status"] == "failed" for item in records),
        "records": records,
    })
    return records


def build_parser() -> argparse.ArgumentParser:
    """创建测试用例生成工具的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="按接口 Markdown 调用 DeepSeek 生成声明式边界测试用例 JSON。")
    parser.add_argument("--input", required=True, type=Path, help="单个接口 Markdown 或包含接口 Markdown 的目录")
    parser.add_argument("--pattern", default="[0-9][0-9][0-9]_*.md", help="目录输入时的文件匹配规则")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output" / "api_test_cases", help="用例 JSON 输出目录")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env", help="模型配置文件")
    parser.add_argument("--retries", type=int, default=3, help="单次模型请求最大次数")
    parser.add_argument("--max-tokens", type=int, default=None, help="可选模型输出 token 限制；默认不传")
    parser.add_argument("--repair-attempts", type=int, default=2, help="schema 校验失败后的模型修复次数")
    parser.add_argument("--no-images", action="store_true", help="不将 Markdown 相对引用的本地图片发送给模型")
    parser.add_argument("--skip-existing", action="store_true", help="已有 *_cases.json 时跳过")
    parser.add_argument("--dry-run", action="store_true", help="只检查接口 Markdown 与图片层级，不调用模型")
    return parser


def main() -> int:
    """执行接口发现、模型调用、schema 校验以及 JSON/Excel 落盘流程。"""
    args = build_parser().parse_args()
    markdown_files = discover_markdown(args.input, args.pattern)
    if args.dry_run:
        report = []
        for path in markdown_files:
            text = path.read_text(encoding="utf-8-sig")
            images, warnings = resolve_markdown_images(path, text)
            report.append({"markdown": str(path), "images": [str(image) for image in images], "warnings": warnings})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if any(item["warnings"] for item in report) else 0
    try:
        from openai import OpenAI
    except ImportError as error:
        raise GenerationError("未安装 OpenAI SDK，请先执行：python -m pip install -U openai") from error
    api_key, base_url, model = _model_config(args.env_file.expanduser().resolve())
    # Matches DeepSeek's official OpenAI-SDK initialization pattern.
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=1)
    records = generate_cases(
        markdown_files, args.output, client, model,
        include_images=not args.no_images, retries=args.retries, max_tokens=args.max_tokens,
        repair_attempts=args.repair_attempts, skip_existing=args.skip_existing,
    )
    generated = sum(item["status"] == "generated" for item in records)
    failed = sum(item["status"] == "failed" for item in records)
    print(f"完成：generated={generated}, failed={failed}, output={args.output.expanduser().resolve()}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenerationError as error:
        print(f"ERROR: {error}")
        raise SystemExit(2) from error
