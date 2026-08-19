"""Validate and execute declarative single-endpoint API test-case JSON files."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from api_test_case_spec import (
    MissingEnvironmentError,
    SpecError,
    atomic_write_json,
    load_env_file,
    resolve_value,
    validate_test_spec,
)
from export_test_cases_excel import update_case_workbook_results


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MISSING = object()


class RunnerError(RuntimeError):
    """Raised for runner configuration or execution errors."""


def load_spec(path: Path) -> dict[str, Any]:
    """读取一个用例 JSON 文件并通过统一 schema 校验。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise RunnerError(f"用例 JSON 解析失败：{path}: {error}") from error
    try:
        return validate_test_spec(data)
    except SpecError as error:
        raise RunnerError(f"用例 schema 校验失败：{path}: {error}") from error


def discover_specs(input_path: Path, pattern: str) -> list[Path]:
    """从文件或目录中发现待校验、待执行的用例规范。"""
    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise RunnerError(f"输入路径不存在：{input_path}")
    specs = sorted(path for path in input_path.glob(pattern) if path.is_file())
    if not specs:
        raise RunnerError(f"没有找到匹配 {pattern!r} 的用例 JSON")
    return specs


def required_environment_names(specs: list[Mapping[str, Any]]) -> list[str]:
    """Return the union of declared runtime variables for a test suite."""

    return sorted(
        {
            name
            for spec in specs
            for name in spec.get("required_env", [])
            if isinstance(name, str)
        }
    )


def missing_required_environment(
    specs: list[Mapping[str, Any]],
    environ: Mapping[str, str],
    *,
    base_url: str | None,
) -> list[str]:
    """在任何 HTTP 请求发出前做整批凭据预检，避免产生大量误导性 skipped。"""

    available = dict(environ)
    if base_url:
        available["API_BASE_URL"] = base_url
    return [
        name
        for name in required_environment_names(specs)
        if not isinstance(available.get(name), str) or not available[name].strip()
    ]


def build_url(base_url: str, path: str, path_params: Mapping[str, Any]) -> str:
    """安全拼接目标主机和相对 path，并完成 path 参数 URL 编码。"""
    parsed_base = urllib.parse.urlsplit(base_url)
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.query
        or parsed_base.fragment
        or parsed_base.username
        or parsed_base.password
    ):
        raise RunnerError("API_BASE_URL 必须是无 query/fragment/账号信息的 http/https URL")
    for name, value in path_params.items():
        path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
    remaining = re.findall(r"\{([^{}]+)\}", path)
    if remaining:
        raise RunnerError("path 参数未提供：" + ", ".join(sorted(set(remaining))))
    parsed_path = urllib.parse.urlsplit(path)
    if not path.startswith("/") or path.startswith("//") or parsed_path.scheme or parsed_path.netloc:
        raise RunnerError("请求 path 必须是相对路径")
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def resolve_pairs(items: list[dict[str, Any]], environ: Mapping[str, str]) -> list[tuple[str, Any]]:
    """解析 name/value 数组中的环境变量构造器并转换为键值对。"""
    return [(item["name"], resolve_value(item["value"], environ)) for item in items]


def json_pointer(document: Any, pointer: str) -> Any:
    """按照 JSON Pointer 定位响应字段，路径不存在时返回 MISSING 哨兵。"""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise RunnerError(f"JSON Pointer 必须以 / 开头：{pointer}")
    current = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return MISSING
            if index < 0 or index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def json_type(value: Any) -> str:
    """把 Python 值映射为测试规范使用的 JSON 类型名称。"""
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def result_assertion(name: str, passed: bool, expected: Any, actual: Any, *, sensitive: bool = False) -> dict[str, Any]:
    """构造一条统一的断言结果记录，保留预期值和实际值。"""
    # ``sensitive`` is retained for call-site compatibility. The runner now
    # records original values exactly as requested by the user.
    return {
        "name": name,
        "passed": passed,
        "expected": expected,
        "actual": actual,
    }


def _matches(actual: Any, op: str, expected: Any) -> bool:
    """执行 equals、contains、matches 等通用二元断言操作。"""
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op == "contains":
        return isinstance(actual, (str, list, dict)) and expected in actual
    if op == "not_contains":
        return isinstance(actual, (str, list, dict)) and expected not in actual
    if op == "matches":
        return isinstance(actual, str) and isinstance(expected, str) and re.search(expected, actual) is not None
    raise RunnerError(f"不支持的断言操作：{op}")


def evaluate_response(response: Any, expected: Mapping[str, Any], elapsed_ms: float, environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """把声明式 expected 转成逐项断言结果；本函数不决定用例总状态。"""
    assertions: list[dict[str, Any]] = [
        result_assertion("status_codes", response.status_code in expected["status_codes"], expected["status_codes"], response.status_code)
    ]
    for item in expected["headers"]:
        actual = response.headers.get(item["name"])
        op = item["op"]
        if op == "exists":
            passed, expected_value, actual_value = actual is not None, "present", "present" if actual is not None else "missing"
        else:
            expected_value = resolve_value(item["value"], environ)
            passed = actual is not None and _matches(actual, op, expected_value)
            actual_value = actual
        assertions.append(result_assertion(f"header:{item['name']}", passed, expected_value, actual_value))
    if expected["json"]:
        try:
            body_json = response.json()
            assertions.append(result_assertion("response_json", True, "valid JSON", "valid JSON"))
        except Exception as error:
            body_json = MISSING
            assertions.append(result_assertion("response_json", False, "valid JSON", type(error).__name__))
        if body_json is not MISSING:
            for item in expected["json"]:
                path, op = item["path"], item["op"]
                actual = json_pointer(body_json, path)
                if op == "exists":
                    assertions.append(result_assertion(f"json:{path}", actual is not MISSING, "present", "present" if actual is not MISSING else "missing"))
                elif op == "not_exists":
                    assertions.append(result_assertion(f"json:{path}", actual is MISSING, "missing", "missing" if actual is MISSING else "present"))
                elif op == "type":
                    expected_value = item["value"]
                    assertions.append(result_assertion(f"json:{path}", json_type(actual) == expected_value, expected_value, json_type(actual)))
                elif op == "not_empty":
                    empty = actual is MISSING or actual is None or actual == "" or actual == [] or actual == {}
                    if isinstance(actual, str) and not actual.strip():
                        empty = True
                    assertions.append(result_assertion(f"json:{path}", not empty, "non-empty", "empty" if empty else "non-empty"))
                else:
                    expected_value = resolve_value(item["value"], environ)
                    passed = actual is not MISSING and _matches(actual, op, expected_value)
                    assertions.append(result_assertion(f"json:{path}", passed, expected_value, "<missing>" if actual is MISSING else actual))
    for item in expected["text"]:
        expected_value = resolve_value(item["value"], environ)
        assertions.append(result_assertion(f"text:{item['op']}", _matches(response.text, item["op"], expected_value), expected_value, "evaluated"))
    if expected["max_response_ms"] is not None:
        assertions.append(result_assertion("max_response_ms", elapsed_ms <= expected["max_response_ms"], expected["max_response_ms"], elapsed_ms))
    return assertions


def response_snapshot(response: Any, environ: Mapping[str, str], max_chars: int = 3000) -> dict[str, Any]:
    """截取响应 JSON 或文本，生成长度受控的结果快照。"""
    snapshot: dict[str, Any] = {"content_type": response.headers.get("Content-Type", "")}
    try:
        text = json.dumps(response.json(), ensure_ascii=False)
        if len(text) <= max_chars:
            snapshot["json"] = json.loads(text)
        else:
            snapshot["json_preview"] = text[:max_chars] + "...[TRUNCATED]"
    except Exception:
        snapshot["text_preview"] = response.text[:max_chars]
    return snapshot


def run_case(case: Mapping[str, Any], spec: Mapping[str, Any], base_url: str, environ: Mapping[str, str], session: Any) -> dict[str, Any]:
    """解析并执行一条用例；这是整个项目真正发送 HTTP 请求的关键位置。"""
    request = case["request"]
    display_method = (request.get("method") or spec["interface"]["method"]).upper()
    display_path = request.get("path") or spec["interface"]["path"]
    try:
        path_params = {name: resolve_value(value, environ) for name, value in request["path_params"].items()}
        url = build_url(base_url, display_path, path_params)
        header_pairs = resolve_pairs(request["headers"], environ)
        headers: dict[str, str] = {}
        for name, value in header_pairs:
            if name.lower() in {key.lower() for key in headers}:
                raise RunnerError(f"不支持重复 Header：{name}")
            headers[name] = str(value)
        query = resolve_pairs(request["query"], environ)
        body = request["body"]
        # 到这里才把声明式 JSON 翻译成 requests.Session.request() 参数。
        options: dict[str, Any] = {
            "method": display_method,
            "url": url,
            "headers": headers,
            "params": query,
            "timeout": (request.get("timeout_ms") or spec["execution"]["timeout_ms"]) / 1000,
            "allow_redirects": request.get("follow_redirects", spec["execution"]["follow_redirects"]),
        }
        if body is not None:
            if body["type"] == "form":
                options["data"] = resolve_pairs(body["fields"], environ)
            elif body["type"] == "json":
                options["json"] = resolve_value(body["value"], environ)
            else:
                raw = resolve_value(body["text"], environ)
                if not isinstance(raw, str):
                    raise RunnerError("raw body 解析后必须是字符串")
                options["data"] = raw
                headers.setdefault("Content-Type", body["content_type"])
        started = time.perf_counter()
        response = session.request(**options)  # 唯一的真实网络发送点，调试时可在此打断点。
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        assertions = evaluate_response(response, case["expected"], elapsed_ms, environ)
        return {
            "case_id": case["id"], "title": case["title"], "category": case["category"], "priority": case["priority"],
            "status": "passed" if all(item["passed"] for item in assertions) else "failed",
            "method": display_method, "path": display_path, "http_status": response.status_code, "elapsed_ms": elapsed_ms,
            "assertions": assertions, "response": response_snapshot(response, environ),
        }
    except MissingEnvironmentError as error:
        return {"case_id": case["id"], "title": case["title"], "category": case["category"], "priority": case["priority"], "status": "skipped", "method": display_method, "path": display_path, "missing_env": list(error.names), "error": str(error)}
    except Exception as error:
        return {"case_id": case["id"], "title": case["title"], "category": case["category"], "priority": case["priority"], "status": "error", "method": display_method, "path": display_path, "error": f"{type(error).__name__}: {error}"}


def run_spec(spec: Mapping[str, Any], base_url: str, environ: Mapping[str, str], timeout_ms: int | None = None, session: Any = None) -> dict[str, Any]:
    """执行一个接口的全部 test_cases；传入 fake session 可进行无网络单元测试。"""
    spec = validate_test_spec(dict(spec))
    missing = missing_required_environment([spec], environ, base_url=base_url)
    if missing:
        raise RunnerError("执行前缺少必要环境变量：" + ", ".join(missing))
    if timeout_ms is not None:
        spec["execution"]["timeout_ms"] = timeout_ms
    if session is None:
        try:
            import requests
        except ImportError as error:
            raise RunnerError("未安装 requests，请先执行：python -m pip install -U requests") from error
        session = requests.Session()
    started = datetime.now(timezone.utc)
    results = [run_case(case, spec, base_url, environ, session) for case in spec["test_cases"]]
    counts = {name: sum(item["status"] == name for item in results) for name in ("passed", "failed", "skipped", "error")}
    return {
        "schema_version": "1.0", "interface": spec["interface"], "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(), "summary": {"total": len(results), **counts}, "results": results,
    }


def write_html_report(results_dir: Path, output_html: Path | None = None) -> Path:
    """Generate a dependency-free HTML report for a results directory."""
    from visualize_api_results import build_html_report

    return build_html_report(results_dir, output_html)


def build_parser() -> argparse.ArgumentParser:
    """创建用例校验与自动化测试执行器的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="校验或执行声明式单接口测试用例 JSON。")
    parser.add_argument("--input", required=True, type=Path, help="单个 *_cases.json 或用例目录")
    parser.add_argument("--pattern", default="*_cases.json", help="目录输入时的文件匹配规则")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env", help="运行环境变量文件")
    parser.add_argument("--base-url", default=None, help="覆盖 API_BASE_URL")
    parser.add_argument("--timeout-ms", type=int, default=None, help="覆盖用例 execution.timeout_ms")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output" / "api_test_results", help="测试结果输出目录")
    parser.add_argument("--interfaces-dir", type=Path, help="接口 Markdown/Excel 目录；提供后会将结果回填到对应 Excel")
    parser.add_argument("--execute", action="store_true", help="真正发送 HTTP 请求；默认仅校验 schema")
    return parser


def main() -> int:
    """校验用例；启用 --execute 时执行 HTTP 请求并写入结果和报告。"""
    args = build_parser().parse_args()
    paths = discover_specs(args.input, args.pattern)
    valid: list[tuple[Path, dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            spec = load_spec(path)
            valid.append((path, spec))
            print(f"[VALID] {path} cases={len(spec['test_cases'])}")
        except Exception as error:
            errors.append({"source": str(path), "error": str(error)})
            print(f"[INVALID] {path}: {error}")
    if not args.execute:
        print(f"校验完成：valid={len(valid)}, invalid={len(errors)}；未设置 --execute，不发送 HTTP 请求。")
        return 1 if errors else 0
    environ = load_env_file(args.env_file.expanduser().resolve())
    environ.update(os.environ)
    base_url = args.base_url or environ.get("API_BASE_URL")
    if not base_url:
        raise RunnerError("执行测试需要 --base-url 或 API_BASE_URL")
    missing = missing_required_environment([spec for _, spec in valid], environ, base_url=base_url)
    if missing:
        raise RunnerError("执行前缺少必要环境变量：" + ", ".join(missing))
    output_dir = args.output.expanduser().resolve()
    reports: list[dict[str, Any]] = []
    excel_workbooks: list[str] = []
    for path, spec in valid:
        try:
            report = run_spec(spec, base_url, environ, timeout_ms=args.timeout_ms)
            report["source"] = str(path)
            result_path = output_dir / f"{path.stem}_results.json"
            atomic_write_json(result_path, report)
            reports.append(report)
            interfaces_dir = args.interfaces_dir
            if interfaces_dir is None:
                inferred = path.parent.parent / "interfaces_markdown"
                interfaces_dir = inferred if inferred.is_dir() else None
            if interfaces_dir is not None:
                try:
                    excel_path = update_case_workbook_results(path, result_path, interfaces_dir)
                    excel_workbooks.append(str(excel_path))
                    print(f"[EXCEL] {excel_path}")
                except Exception as error:
                    errors.append({"source": f"excel:{path.name}", "error": f"{type(error).__name__}: {error}"})
                    print(f"[ERROR] excel {path.name}: {error}")
            summary = report["summary"]
            print(f"[RESULT] {path.name}: passed={summary['passed']}, failed={summary['failed']}, skipped={summary['skipped']}, error={summary['error']}")
        except Exception as error:
            errors.append({"source": str(path), "error": str(error)})
            print(f"[ERROR] {path}: {error}")
    totals = {key: sum(report["summary"][key] for report in reports) for key in ("total", "passed", "failed", "skipped", "error")}
    report_path = None
    if reports:
        try:
            report_path = write_html_report(output_dir)
            print(f"[REPORT] {report_path}")
        except Exception as error:
            errors.append({"source": "visualization", "error": f"{type(error).__name__}: {error}"})
            print(f"[ERROR] visualization: {error}")
    atomic_write_json(
        output_dir / "run_manifest.json",
        {
            "summary": totals,
            "file_errors": errors,
            "reports": [report.get("source") for report in reports],
            "excel_workbooks": excel_workbooks,
            "report": str(report_path) if report_path else None,
        },
    )
    return 1 if errors or totals["failed"] or totals["error"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as error:
        print(f"ERROR: {error}")
        raise SystemExit(2) from error
