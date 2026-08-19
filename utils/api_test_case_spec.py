"""Shared contract for generated single-endpoint API test cases.

The contract is deliberately declarative: models produce JSON data only and
the runner interprets a small, validated whitelist of request/assertion fields.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "1.0"
# 这些集合不是展示用枚举，而是执行器真正接受的白名单。模型多生成一个
# 未支持的取值，validate_test_spec() 就会在发送 HTTP 请求前拒绝它。
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
CATEGORIES = {
    "positive", "required", "length", "type", "enum", "auth",
    "protocol", "encoding", "security", "other",
}
PRIORITIES = {"P0", "P1", "P2"}
HEADER_OPS = {"exists", "equals", "contains", "not_contains", "matches"}
JSON_OPS = {
    "exists", "not_exists", "equals", "not_equals", "type", "not_empty",
    "contains", "matches",
}
TEXT_OPS = {"equals", "contains", "not_contains", "matches"}
JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TEMPLATE_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
LEGACY_ENV_VALUE = re.compile(r'^\{\$env\s*:\s*["\'](?P<name>[A-Za-z_][A-Za-z0-9_]*)["\']\s*\}$')
LEGACY_TEMPLATE_VALUE = re.compile(r'^\{\$template\s*:\s*["\'](?P<template>.*)["\']\s*\}$')
LEGACY_REPEAT_VALUE = re.compile(r'^\{\$repeat\s*:\s*(?P<repeat>\{.*\})\s*\}$')
SAFE_FILE_STEM = re.compile(r"[^0-9A-Za-z_]+")


class SpecError(ValueError):
    """Raised when a case specification does not follow the v1 contract."""


class MissingEnvironmentError(RuntimeError):
    """Raised when a case needs a runtime environment variable that is absent."""

    def __init__(self, names: set[str]) -> None:
        """记录缺失的环境变量名称，并生成便于展示的错误消息。"""
        self.names = tuple(sorted(names))
        super().__init__("缺少环境变量：" + ", ".join(self.names))


def load_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv files without changing process env."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def merged_environment(env_file: Path | None = None) -> dict[str, str]:
    """合并可选 dotenv 文件与当前进程环境，进程环境中的值优先。"""
    values = load_env_file(env_file) if env_file else {}
    values.update(os.environ)
    return values


def atomic_write_json(path: Path, data: Any) -> None:
    """先写临时文件再原子替换目标文件，避免留下半写入的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def safe_file_stem(text: str) -> str:
    """把任意文本转换为可安全用于生成文件名的英文下划线形式。"""
    stem = SAFE_FILE_STEM.sub("_", text).strip("_").lower()
    return stem or "interface"


def _require_object(value: Any, location: str) -> dict[str, Any]:
    """确认指定位置的值是 JSON object，并返回其字典形式。"""
    if not isinstance(value, dict):
        raise SpecError(f"{location} 必须是 object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], location: str) -> None:
    """拒绝白名单之外的字段，防止模型输出被执行器静默忽略。"""
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SpecError(f"{location} 包含不支持的字段：{', '.join(unknown)}")


def _validate_relative_path(path: str, location: str, *, allow_template: bool = False) -> None:
    """校验请求路径必须是站内相对路径，不能携带主机、查询串或片段。"""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise SpecError(f"{location} 必须是以 / 开头的相对 path")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise SpecError(f"{location} 不允许完整 URL、query 或 fragment")
    if not allow_template and "{" in path:
        raise SpecError(f"{location} 不允许未声明的 path 参数占位符")


def _validate_value(value: Any, location: str) -> None:
    """递归校验普通 JSON 值及 $env、$template、$repeat 三种值构造器。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_value(item, f"{location}[{index}]")
        return
    if not isinstance(value, dict):
        raise SpecError(f"{location} 不是合法 JSON 值")
    reserved = {key for key in value if key.startswith("$")}
    if not reserved:
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpecError(f"{location} 的对象键必须是字符串")
            _validate_value(item, f"{location}.{key}")
        return
    if set(value) == {"$env"}:
        if not isinstance(value["$env"], str) or not ENV_NAME.fullmatch(value["$env"]):
            raise SpecError(f"{location}.$env 必须是环境变量名称")
        return
    if set(value) == {"$template"}:
        if not isinstance(value["$template"], str):
            raise SpecError(f"{location}.$template 必须是字符串")
        return
    if set(value) == {"$repeat"} and isinstance(value["$repeat"], dict):
        repeat = value["$repeat"]
        if set(repeat) != {"text", "count"}:
            raise SpecError(f"{location}.$repeat 只能包含 text 和 count")
        if not isinstance(repeat["text"], str) or len(repeat["text"]) > 100:
            raise SpecError(f"{location}.$repeat.text 必须是不超过 100 字符的字符串")
        count = repeat["count"]
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 100000:
            raise SpecError(f"{location}.$repeat.count 必须是 0~100000 的整数")
        return
    raise SpecError(f"{location} 使用了不支持的值构造器")


def _legacy_constructor(value: str) -> Any:
    """Convert legacy string constructors emitted by early prompts into objects."""
    stripped = value.strip()
    env_match = LEGACY_ENV_VALUE.fullmatch(stripped)
    if env_match:
        return {"$env": env_match.group("name")}
    template_match = LEGACY_TEMPLATE_VALUE.fullmatch(stripped)
    if template_match:
        return {"$template": template_match.group("template")}
    repeat_match = LEGACY_REPEAT_VALUE.fullmatch(stripped)
    if repeat_match:
        try:
            repeat = json.loads(repeat_match.group("repeat"))
        except json.JSONDecodeError:
            return value
        if isinstance(repeat, dict):
            return {"$repeat": repeat}
    return value


def _collect_env_names(value: Any) -> set[str]:
    """递归收集一个用例值中引用的全部环境变量名称。"""
    names: set[str] = set()
    if isinstance(value, str):
        legacy = _legacy_constructor(value)
        if legacy is not value:
            names.update(_collect_env_names(legacy))
    elif isinstance(value, list):
        for item in value:
            names.update(_collect_env_names(item))
    elif isinstance(value, dict):
        if set(value) == {"$env"}:
            names.add(value["$env"])
        elif set(value) == {"$template"}:
            names.update(TEMPLATE_ENV.findall(value["$template"]))
        else:
            for item in value.values():
                names.update(_collect_env_names(item))
    return names


def _validate_name_value_list(value: Any, location: str, *, unique_names: bool = False) -> None:
    """校验 Header、Query 或表单字段使用的 name/value 数组结构。"""
    if not isinstance(value, list):
        raise SpecError(f"{location} 必须是 name/value 数组")
    names: set[str] = set()
    for index, item in enumerate(value):
        item_location = f"{location}[{index}]"
        item = _require_object(item, item_location)
        _reject_unknown(item, {"name", "value"}, item_location)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise SpecError(f"{item_location}.name 必须是非空字符串")
        normalized_name = item["name"].casefold()
        if unique_names and normalized_name in names:
            raise SpecError(f"{location} contains duplicate name: {item['name']}")
        names.add(normalized_name)
        if "value" not in item:
            raise SpecError(f"{item_location}.value 缺失")
        _validate_value(item["value"], f"{item_location}.value")


def _validate_request(request: Any, location: str, interface: Mapping[str, Any]) -> None:
    """校验单条用例的 HTTP 请求定义，并限制其为执行器支持的字段。"""
    request = _require_object(request, location)
    _reject_unknown(
        request,
        {"method", "path", "headers", "query", "path_params", "body", "timeout_ms", "follow_redirects"},
        location,
    )
    method = request.get("method")
    if method is not None and (not isinstance(method, str) or method.upper() not in METHODS):
        raise SpecError(f"{location}.method 不在支持的 HTTP Method 范围内")
    path = request.get("path", interface["path"])
    if path is not None:
        _validate_relative_path(path, f"{location}.path", allow_template=True)
    _validate_name_value_list(request.get("headers", []), f"{location}.headers", unique_names=True)
    _validate_name_value_list(request.get("query", []), f"{location}.query")
    path_params = request.get("path_params", {})
    path_params = _require_object(path_params, f"{location}.path_params")
    for name, value in path_params.items():
        if not isinstance(name, str) or not name:
            raise SpecError(f"{location}.path_params 的参数名必须是非空字符串")
        _validate_value(value, f"{location}.path_params.{name}")
    body = request.get("body")
    if body is not None:
        body = _require_object(body, f"{location}.body")
        body_type = body.get("type")
        if body_type == "form":
            _reject_unknown(body, {"type", "fields"}, f"{location}.body")
            _validate_name_value_list(body.get("fields"), f"{location}.body.fields")
        elif body_type == "json":
            _reject_unknown(body, {"type", "value"}, f"{location}.body")
            if "value" not in body:
                raise SpecError(f"{location}.body.value 缺失")
            _validate_value(body["value"], f"{location}.body.value")
        elif body_type == "raw":
            _reject_unknown(body, {"type", "content_type", "text"}, f"{location}.body")
            if not isinstance(body.get("content_type"), str) or not body["content_type"]:
                raise SpecError(f"{location}.body.content_type 必须是非空字符串")
            if "text" not in body:
                raise SpecError(f"{location}.body.text 缺失")
            _validate_value(body["text"], f"{location}.body.text")
        else:
            raise SpecError(f"{location}.body.type 只支持 form/json/raw")
    timeout = request.get("timeout_ms")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120000
    ):
        raise SpecError(f"{location}.timeout_ms 必须是 1~120000 的整数或 null")
    if "follow_redirects" in request and not isinstance(request["follow_redirects"], bool):
        raise SpecError(f"{location}.follow_redirects 必须是 boolean")


def _validate_assertion_list(value: Any, location: str, kind: str) -> None:
    """按 header、json 或 text 类型校验断言数组及其操作符。"""
    if not isinstance(value, list):
        raise SpecError(f"{location} 必须是数组")
    for index, assertion in enumerate(value):
        assertion_location = f"{location}[{index}]"
        assertion = _require_object(assertion, assertion_location)
        if kind == "header":
            _reject_unknown(assertion, {"name", "op", "value"}, assertion_location)
            op_set = HEADER_OPS
        elif kind == "json":
            _reject_unknown(assertion, {"path", "op", "value"}, assertion_location)
            op_set = JSON_OPS
        else:
            _reject_unknown(assertion, {"op", "value"}, assertion_location)
            op_set = TEXT_OPS
        key = "name" if kind == "header" else "path" if kind == "json" else None
        if key and (not isinstance(assertion.get(key), str) or not assertion[key]):
            raise SpecError(f"{assertion_location}.{key} 必须是非空字符串")
        op = assertion.get("op")
        if op not in op_set:
            raise SpecError(f"{assertion_location}.op 不支持：{op!r}")
        needs_value = op not in {"exists", "not_exists", "not_empty"}
        if needs_value and "value" not in assertion:
            raise SpecError(f"{assertion_location}.value 缺失")
        if "value" in assertion:
            if kind == "json" and op == "type":
                if assertion["value"] not in JSON_TYPES:
                    raise SpecError(f"{assertion_location}.value 不是支持的 JSON 类型")
            else:
                _validate_value(assertion["value"], f"{assertion_location}.value")


def _validate_expected(expected: Any, location: str) -> None:
    """校验预期状态码、响应断言和最大响应时间配置。"""
    expected = _require_object(expected, location)
    _reject_unknown(expected, {"status_codes", "headers", "json", "text", "max_response_ms"}, location)
    statuses = expected.get("status_codes")
    if not isinstance(statuses, list) or not statuses or any(
        not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599
        for status in statuses
    ):
        raise SpecError(f"{location}.status_codes 必须是非空 HTTP 状态码数组")
    _validate_assertion_list(expected.get("headers", []), f"{location}.headers", "header")
    _validate_assertion_list(expected.get("json", []), f"{location}.json", "json")
    _validate_assertion_list(expected.get("text", []), f"{location}.text", "text")
    max_response = expected.get("max_response_ms")
    if max_response is not None and (
        not isinstance(max_response, int) or isinstance(max_response, bool) or max_response <= 0
    ):
        raise SpecError(f"{location}.max_response_ms 必须是正整数或 null")


def validate_test_spec(spec: Any) -> dict[str, Any]:
    """校验一个单接口用例文档；这是模型输出进入执行器前的信任边界。"""
    spec = _require_object(spec, "root")
    _reject_unknown(spec, {"schema_version", "interface", "execution", "required_env", "test_cases"}, "root")
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise SpecError(f"schema_version 必须为 {SCHEMA_VERSION}")

    # 第一层：接口身份只能包含 method + 相对 path，禁止模型直接指定目标主机。
    interface = _require_object(spec.get("interface"), "interface")
    _reject_unknown(interface, {"name", "operation_id", "method", "path"}, "interface")
    if not isinstance(interface.get("name"), str) or not interface["name"].strip():
        raise SpecError("interface.name 必须是非空字符串")
    if interface.get("operation_id") is not None and not isinstance(interface["operation_id"], str):
        raise SpecError("interface.operation_id 必须是字符串或 null")
    if not isinstance(interface.get("method"), str) or interface["method"].upper() not in METHODS:
        raise SpecError("interface.method 不在支持的 HTTP Method 范围内")
    interface["method"] = interface["method"].upper()
    _validate_relative_path(interface.get("path"), "interface.path", allow_template=True)

    # 第二层：限制超时和重定向，避免生成结果改变执行器的安全边界。
    execution = _require_object(spec.get("execution"), "execution")
    _reject_unknown(execution, {"timeout_ms", "follow_redirects"}, "execution")
    timeout = execution.get("timeout_ms")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120000:
        raise SpecError("execution.timeout_ms 必须是 1~120000 的整数")
    if not isinstance(execution.get("follow_redirects"), bool):
        raise SpecError("execution.follow_redirects 必须是 boolean")

    # 第三层：凭据只声明变量名；真实值在运行时由环境注入。
    required_env = spec.get("required_env")
    if not isinstance(required_env, list) or not all(
        isinstance(name, str) and ENV_NAME.fullmatch(name) for name in required_env
    ):
        raise SpecError("required_env 必须是环境变量名称数组")
    if len(required_env) != len(set(required_env)):
        raise SpecError("required_env 不允许重复")
    if "API_BASE_URL" not in required_env:
        raise SpecError("required_env 必须包含 API_BASE_URL")

    # 第四层：逐条验证请求和断言，同时保证 case id 唯一。
    cases = spec.get("test_cases")
    if not isinstance(cases, list) or not cases:
        raise SpecError("test_cases 必须是非空数组")
    case_ids: set[str] = set()
    for index, case in enumerate(cases):
        location = f"test_cases[{index}]"
        case = _require_object(case, location)
        _reject_unknown(case, {"id", "title", "category", "priority", "boundary", "assumption", "request", "expected"}, location)
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise SpecError(f"{location}.id 必须是非空字符串")
        if case_id in case_ids:
            raise SpecError(f"测试用例 id 重复：{case_id}")
        case_ids.add(case_id)
        if not isinstance(case.get("title"), str) or not case["title"].strip():
            raise SpecError(f"{location}.title 必须是非空字符串")
        if case.get("category") not in CATEGORIES:
            raise SpecError(f"{location}.category 不支持")
        if case.get("priority") not in PRIORITIES:
            raise SpecError(f"{location}.priority 必须是 P0/P1/P2")
        boundary = _require_object(case.get("boundary"), f"{location}.boundary")
        _reject_unknown(boundary, {"target", "rule", "value"}, f"{location}.boundary")
        if not isinstance(boundary.get("target"), str) or not boundary["target"]:
            raise SpecError(f"{location}.boundary.target 必须是非空字符串")
        if not isinstance(boundary.get("rule"), str) or not boundary["rule"]:
            raise SpecError(f"{location}.boundary.rule 必须是非空字符串")
        if "value" in boundary:
            _validate_value(boundary["value"], f"{location}.boundary.value")
        if case.get("assumption") is not None and not isinstance(case["assumption"], str):
            raise SpecError(f"{location}.assumption 必须是字符串或 null")
        _validate_request(case.get("request"), f"{location}.request", interface)
        _validate_expected(case.get("expected"), f"{location}.expected")

    # 跨字段检查：用例中引用的每个变量都必须显式出现在 required_env。
    undeclared = _collect_env_names(cases) - set(required_env)
    if undeclared:
        raise SpecError("以下环境变量未声明在 required_env：" + ", ".join(sorted(undeclared)))
    return spec


def resolve_value(value: Any, environ: Mapping[str, str]) -> Any:
    """执行时递归展开 `$env`、`$template`、`$repeat` 三种受控构造器。"""
    if isinstance(value, str):
        legacy = _legacy_constructor(value)
        if legacy is not value:
            return resolve_value(legacy, environ)
        return value
    if isinstance(value, list):
        return [resolve_value(item, environ) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$env"}:
        name = value["$env"]
        if not environ.get(name):
            raise MissingEnvironmentError({name})
        return environ[name]
    if set(value) == {"$template"}:
        template = value["$template"]
        missing = {name for name in TEMPLATE_ENV.findall(template) if not environ.get(name)}
        if missing:
            raise MissingEnvironmentError(missing)
        return TEMPLATE_ENV.sub(lambda match: environ[match.group(1)], template)
    if set(value) == {"$repeat"}:
        repeat = value["$repeat"]
        return repeat["text"] * repeat["count"]
    return {key: resolve_value(item, environ) for key, item in value.items()}
