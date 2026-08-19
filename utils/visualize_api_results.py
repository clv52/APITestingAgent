"""Aggregate API test results into a self-contained HTML report.

The report intentionally has no third-party dependencies. It reads the JSON
files written by run_api_test_cases.py and renders summary metrics, status
bars, response-time rankings, and abnormal-case details as static HTML/CSS.
"""

from __future__ import annotations

import argparse
import html
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


STATUS_ORDER = ("passed", "failed", "error", "skipped")
STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "error": "异常",
    "skipped": "跳过",
}
STATUS_COLORS = {
    "passed": "#16a34a",
    "failed": "#dc2626",
    "error": "#ea580c",
    "skipped": "#64748b",
}


def _text(value: Any) -> str:
    """把可空值稳定转换为展示文本。"""
    return "" if value is None else str(value)


def _escape(value: Any) -> str:
    """转义动态内容，避免生成的 HTML 被结果数据注入标签。"""
    return html.escape(_text(value), quote=True)


def _load_json(path: Path) -> dict[str, Any]:
    """读取结果 JSON，并把读取错误转换为可进入报告的错误记录。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        return {"_load_error": f"{type(error).__name__}: {error}", "source": str(path)}
    if not isinstance(data, dict):
        return {"_load_error": "result JSON root must be an object", "source": str(path)}
    return data


def _summary_counts(report: dict[str, Any]) -> dict[str, int]:
    """从报告摘要或逐条结果中计算各执行状态数量。"""
    summary = report.get("summary") or {}
    counts = {status: 0 for status in STATUS_ORDER}
    if isinstance(summary, dict) and any(status in summary for status in STATUS_ORDER):
        for status in STATUS_ORDER:
            value = summary.get(status, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[status] = value
    else:
        results = report.get("results")
        if isinstance(results, list):
            for item in results:
                status = item.get("status")
                if status in counts:
                    counts[status] += 1
    return counts


def _report_label(report: dict[str, Any]) -> str:
    """提取接口显示名称，缺失时回退到来源文件名。"""
    interface = report.get("interface")
    if isinstance(interface, dict) and interface.get("name"):
        return _text(interface["name"])
    source = report.get("source")
    if source:
        return Path(str(source)).stem
    return "未知接口"


def _interface_path(report: dict[str, Any]) -> str:
    """拼接接口的 HTTP Method 与 path，供报告表格展示。"""
    interface = report.get("interface")
    if isinstance(interface, dict):
        method = interface.get("method", "")
        path = interface.get("path", "")
        if path:
            return f"{method} {path}".strip()
    source = report.get("source")
    if source:
        return _text(source)
    return "-"


def _record_assertion_failures(result: dict[str, Any]) -> list[dict[str, Any]]:
    """从单条用例结果中提取失败断言及其预期值、实际值。"""
    failures: list[dict[str, Any]] = []
    for assertion in result.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        if assertion.get("passed") is False:
            failures.append(
                {
                    "name": assertion.get("name", ""),
                    "expected": assertion.get("expected"),
                    "actual": assertion.get("actual"),
                }
            )
    return failures


def _flatten_reports(reports: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """将多个接口报告压平成用例记录列表，并单独收集载入错误。"""
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    for report in reports:
        if "_load_error" in report:
            load_errors.append(report)
            continue
        label = _report_label(report)
        interface_path = _interface_path(report)
        for result in report.get("results") or []:
            if not isinstance(result, dict):
                continue
            status = result.get("status", "error")
            records.append(
                {
                    "interface": label,
                    "interface_path": interface_path,
                    "case_id": result.get("case_id", ""),
                    "title": result.get("title", ""),
                    "category": result.get("category", ""),
                    "priority": result.get("priority", ""),
                    "status": status if status in STATUS_LABELS else "error",
                    "method": result.get("method", ""),
                    "path": result.get("path", ""),
                    "http_status": result.get("http_status"),
                    "elapsed_ms": result.get("elapsed_ms"),
                    "missing_env": result.get("missing_env"),
                    "error": result.get("error", ""),
                    "assertion_failures": _record_assertion_failures(result),
                }
            )
    return records, load_errors


def _percentile(values: list[float], percentile: float) -> float:
    """计算一组响应耗时的离散百分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个接口结果压平成报表模型；所有页面指标都从这里派生。"""
    counts = {status: 0 for status in STATUS_ORDER}
    records, load_errors = _flatten_reports(reports)
    for record in records:
        counts[record["status"]] += 1

    elapsed_values = [
        float(record["elapsed_ms"])
        for record in records
        if isinstance(record["elapsed_ms"], (int, float)) and not isinstance(record["elapsed_ms"], bool)
    ]
    total = sum(counts.values())
    passed = counts["passed"]
    anomaly = counts["failed"] + counts["error"] + counts["skipped"]
    metrics = {
        "total": total,
        "passed": passed,
        "failed": counts["failed"],
        "error": counts["error"],
        "skipped": counts["skipped"],
        "anomaly": anomaly,
        "pass_rate": (passed / total * 100) if total else 0.0,
        "anomaly_rate": (anomaly / total * 100) if total else 0.0,
        "avg_ms": round(statistics.fmean(elapsed_values)) if elapsed_values else 0,
        "p50_ms": round(_percentile(elapsed_values, 0.5)) if elapsed_values else 0,
        "p95_ms": round(_percentile(elapsed_values, 0.95)) if elapsed_values else 0,
        "max_ms": round(max(elapsed_values)) if elapsed_values else 0,
    }

    interface_rows: list[dict[str, Any]] = []
    interface_map: dict[str, dict[str, Any]] = {}
    for report in reports:
        if "_load_error" in report:
            continue
        label = _report_label(report)
        row = interface_map.setdefault(
            label,
            {
                "name": label,
                "path": _interface_path(report),
                **{status: 0 for status in STATUS_ORDER},
            },
        )
        for status, count in _summary_counts(report).items():
            row[status] += count
    for row in interface_map.values():
        row["total"] = sum(row[status] for status in STATUS_ORDER)
        row["pass_rate"] = (row["passed"] / row["total"] * 100) if row["total"] else 0.0
        interface_rows.append(row)
    interface_rows.sort(key=lambda item: (-item["total"], item["name"]))

    category_rows = _distribution_rows(records, "category")
    priority_rows = _distribution_rows(records, "priority")

    exceptions = [
        record for record in records
        if record["status"] in {"failed", "error", "skipped"}
    ]
    exceptions.sort(
        key=lambda item: (
            0 if item["status"] == "error" else 1 if item["status"] == "failed" else 2,
            -(item["elapsed_ms"] or 0),
        )
    )

    return {
        "counts": counts,
        "metrics": metrics,
        "records": records,
        "interface_rows": interface_rows,
        "category_rows": category_rows,
        "priority_rows": priority_rows,
        "exceptions": exceptions,
        "load_errors": load_errors,
    }


def _distribution_rows(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """按分类或优先级聚合状态计数和通过率。"""
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        label = _text(record.get(key) or "other")
        row = grouped.setdefault(
            label,
            {"name": label, **{status: 0 for status in STATUS_ORDER}},
        )
        row[record["status"]] += 1
    rows: list[dict[str, Any]] = []
    for row in grouped.values():
        row["total"] = sum(row[status] for status in STATUS_ORDER)
        row["pass_rate"] = (row["passed"] / row["total"] * 100) if row["total"] else 0.0
        rows.append(row)
    rows.sort(key=lambda item: (-item["total"], item["name"]))
    return rows


def _bar_width(value: float, maximum: float) -> float:
    """把数值归一化为 0~100 的 CSS 柱状图宽度。"""
    if maximum <= 0:
        return 0.0
    return max(0.0, min(100.0, value / maximum * 100))


def _stacked_bar(counts: dict[str, int]) -> str:
    """根据各状态数量生成一段堆叠状态条 HTML。"""
    total = sum(counts.values())
    if not total:
        return '<div class="stack"><span class="stack-empty"></span></div>'
    parts: list[str] = []
    for status in STATUS_ORDER:
        count = counts[status]
        if count <= 0:
            continue
        width = count / total * 100
        parts.append(
            f'<span class="stack-seg" style="width:{width:.2f}%;background:{STATUS_COLORS[status]}"></span>'
        )
    return f'<div class="stack">{"".join(parts)}</div>'


def _donut(counts: dict[str, int]) -> str:
    """使用 conic-gradient 生成状态分布圆环图 HTML。"""
    total = sum(counts.values())
    if not total:
        return '<div class="donut donut-empty" style="background:#e2e8f0"></div>'
    segments: list[str] = []
    start = 0.0
    for status in STATUS_ORDER:
        count = counts[status]
        if count <= 0:
            continue
        end = start + count / total * 360
        segments.append(f"{STATUS_COLORS[status]} {start:.2f}deg {end:.2f}deg")
        start = end
    return f'<div class="donut" style="background:conic-gradient({", ".join(segments)})"></div>'


def _fmt_ms(value: Any) -> str:
    """将毫秒数格式化为易读的毫秒或秒字符串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1000:
        return f"{number / 1000:.2f}s"
    return f"{number:,.0f}ms"


def _summary_card(label: str, value: Any, suffix: str = "", color: str = "") -> str:
    """生成一张摘要指标卡片的 HTML。"""
    style = f' style="--card-color:{color}"' if color else ""
    return (
        f'<div class="metric-card"{style}>'
        f'<div class="metric-label">{_escape(label)}</div>'
        f'<div class="metric-value">{_escape(value)}<span>{_escape(suffix)}</span></div>'
        "</div>"
    )


def _legend() -> str:
    """生成通过、失败、异常和跳过状态的图例 HTML。"""
    parts = []
    for status in STATUS_ORDER:
        parts.append(
            f'<span class="legend-item"><i style="background:{STATUS_COLORS[status]}"></i>'
            f"{_escape(STATUS_LABELS[status])}</span>"
        )
    return f'<div class="legend">{"".join(parts)}</div>'


def _distribution_table(rows: list[dict[str, Any]], title: str) -> str:
    """把聚合分布数据渲染为带占比条的 HTML 表格。"""
    if not rows:
        return f'<h3>{_escape(title)}</h3><p class="muted">暂无数据</p>'
    body: list[str] = []
    for row in rows:
        max_count = max(item["total"] for item in rows)
        width = _bar_width(row["total"], max_count)
        body.append(
            "<tr>"
            f"<td>{_escape(row['name'])}</td>"
            f"<td>{row['total']}</td>"
            f"<td>{row['passed']}</td>"
            f"<td>{row['failed'] + row['error'] + row['skipped']}</td>"
            f"<td>{row['pass_rate']:.1f}%</td>"
            f'<td class="bar-cell"><div class="bar-track"><span style="width:{width:.1f}%"></span></div></td>'
            "</tr>"
        )
    return (
        f"<h3>{_escape(title)}</h3>"
        '<table><thead><tr><th>项</th><th>总数</th><th>通过</th><th>异常</th>'
        "<th>通过率</th><th>占比</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _exception_table(exceptions: list[dict[str, Any]]) -> str:
    """渲染失败、异常与跳过用例的原因明细表。"""
    if not exceptions:
        return '<div class="success-banner">所有执行完成的用例均通过，未发现异常用例。</div>'
    body: list[str] = []
    for item in exceptions:
        if item["status"] == "failed":
            reason = "；".join(failure["name"] for failure in item["assertion_failures"][:6])
            if not reason:
                reason = "断言失败"
        elif item["status"] == "error":
            reason = _text(item.get("error") or "请求执行异常")
        else:
            missing = item.get("missing_env")
            if missing:
                reason = "缺少环境变量：" + ", ".join(_text(name) for name in missing)
            else:
                reason = _text(item.get("error") or "用例被跳过")
        status_color = STATUS_COLORS[item["status"]]
        body.append(
            "<tr>"
            f"<td>{_escape(item['interface'])}<br><small>{_escape(item['interface_path'])}</small></td>"
            f"<td>{_escape(item['case_id'])}<br><small>{_escape(item['title'])}</small></td>"
            f"<td>{_escape(item['priority'])}</td>"
            f"<td>{_escape(item['category'])}</td>"
            f'<td><span class="status-pill" style="background:{status_color}">{_escape(STATUS_LABELS[item["status"]])}</span></td>'
            f"<td>{_escape(item['http_status'] or '-')}</td>"
            f"<td>{_fmt_ms(item['elapsed_ms'])}</td>"
            f'<td class="reason-cell">{_escape(reason)}</td>'
            "</tr>"
        )
    return (
        '<table class="exception-table"><thead><tr>'
        "<th>接口</th><th>用例</th><th>优先级</th><th>分类</th><th>状态</th>"
        "<th>HTTP</th><th>耗时</th><th>原因</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _response_time_rows(records: list[dict[str, Any]]) -> str:
    """生成响应耗时最慢的前 20 条用例表格。"""
    with_time = [
        record for record in records
        if isinstance(record["elapsed_ms"], (int, float)) and not isinstance(record["elapsed_ms"], bool)
    ]
    if not with_time:
        return '<h3>响应耗时排行</h3><p class="muted">暂无响应耗时数据</p>'
    with_time.sort(key=lambda item: float(item["elapsed_ms"]), reverse=True)
    max_ms = max(float(item["elapsed_ms"]) for item in with_time[:20])
    body: list[str] = []
    for item in with_time[:20]:
        elapsed = float(item["elapsed_ms"])
        width = _bar_width(elapsed, max_ms)
        color = "#dc2626" if elapsed > 3000 else "#ea580c" if elapsed > 1000 else "#16a34a"
        body.append(
            "<tr>"
            f"<td>{_escape(item['interface'])}</td>"
            f"<td>{_escape(item['case_id'])}</td>"
            f"<td>{_escape(item['status'])}</td>"
            f"<td>{_fmt_ms(elapsed)}</td>"
            f'<td class="bar-cell"><div class="bar-track"><span style="width:{width:.1f}%;background:{color}"></span></div></td>'
            "</tr>"
        )
    return (
        "<h3>响应耗时排行（Top 20）</h3>"
        '<table><thead><tr><th>接口</th><th>用例</th><th>状态</th><th>耗时</th>'
        "<th>相对耗时</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def build_html_report(
    input_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    title: str = "API 接口测试报告",
) -> Path:
    """读取全部 `*_results.json`，生成无需服务器即可打开的单文件 HTML。"""
    input_dir = Path(input_dir).expanduser().resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Results directory does not exist: {input_dir}")
    result_paths = sorted(path for path in input_dir.glob("*_results.json") if path.is_file())
    reports = [_load_json(path) for path in result_paths]
    aggregated = _aggregate(reports)
    metrics = aggregated["metrics"]
    counts = aggregated["counts"]

    if output_path is None:
        output_path = input_dir / "api_test_report.html"
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    summary_cards = (
        _summary_card("用例总数", metrics["total"], " 个")
        + _summary_card("通过", metrics["passed"], " 个", STATUS_COLORS["passed"])
        + _summary_card("失败", metrics["failed"], " 个", STATUS_COLORS["failed"])
        + _summary_card("异常", metrics["error"], " 个", STATUS_COLORS["error"])
        + _summary_card("跳过", metrics["skipped"], " 个", STATUS_COLORS["skipped"])
        + _summary_card("通过率", f"{metrics['pass_rate']:.1f}", "%", "#0f766e")
        + _summary_card("平均耗时", _fmt_ms(metrics["avg_ms"]), "", "#2563eb")
        + _summary_card("P95 耗时", _fmt_ms(metrics["p95_ms"]), "", "#7c3aed")
    )

    load_error_html = ""
    if aggregated["load_errors"]:
        rows = "".join(
            f"<tr><td>{_escape(item.get('source', ''))}</td><td>{_escape(item.get('_load_error', ''))}</td></tr>"
            for item in aggregated["load_errors"]
        )
        load_error_html = (
            '<section class="panel"><h3>结果文件读取异常</h3>'
            '<table><thead><tr><th>文件</th><th>错误</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></section>"
        )

    file_manifest = input_dir / "run_manifest.json"
    file_errors_html = ""
    if file_manifest.is_file():
        manifest = _load_json(file_manifest)
        file_errors = manifest.get("file_errors") or []
        if file_errors:
            rows = "".join(
                f"<tr><td>{_escape(item.get('source', ''))}</td><td>{_escape(item.get('error', ''))}</td></tr>"
                for item in file_errors
            )
            file_errors_html = (
                '<section class="panel"><h3>接口执行文件异常</h3>'
                '<table><thead><tr><th>文件</th><th>错误</th></tr></thead>'
                f"<tbody>{rows}</tbody></table></section>"
            )

    interface_table = _distribution_table(aggregated["interface_rows"], "接口维度")
    category_table = _distribution_table(aggregated["category_rows"], "测试分类维度")
    priority_table = _distribution_table(aggregated["priority_rows"], "优先级维度")

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>
:root {{
  --bg: #f1f5f9;
  --panel: #ffffff;
  --text: #0f172a;
  --muted: #64748b;
  --line: #e2e8f0;
  --accent: #0f766e;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 32px 20px;
  background: var(--bg);
  color: var(--text);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif;
  line-height: 1.5;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; }}
header {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
h2 {{ font-size: 20px; margin: 0 0 14px; }}
h3 {{ font-size: 16px; margin: 0 0 12px; }}
.muted {{ color: var(--muted); font-size: 13px; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }}
.metric-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 1px 2px rgb(15 23 42 / 0.04); }}
.metric-card:first-child {{ grid-column: span 2; }}
.metric-label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
.metric-value {{ font-size: 30px; font-weight: 700; }}
.metric-value span {{ font-size: 14px; color: var(--muted); font-weight: 400; margin-left: 4px; }}
.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 20px; margin-bottom: 16px; }}
.grid-2 {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 16px; }}
.legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin-top: 12px; color: var(--muted); font-size: 13px; }}
.legend i {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }}
.donut {{ width: 180px; height: 180px; border-radius: 50%; position: relative; }}
.donut-empty {{ opacity: 0.4; }}
.donut::after {{ content: ""; position: absolute; inset: 40px; background: var(--panel); border-radius: 50%; }}
.donut-box {{ display: flex; align-items: center; gap: 24px; }}
.donut-wrap {{ position: relative; width: 180px; height: 180px; }}
.donut-center {{ position: absolute; inset: 0; display: grid; place-items: center; text-align: center; z-index: 1; }}
.donut-center strong {{ display: block; font-size: 24px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
td small {{ color: var(--muted); display: block; }}
.stack {{ height: 18px; border-radius: 6px; overflow: hidden; display: flex; background: var(--line); }}
.stack-seg {{ height: 100%; }}
.stack-empty {{ width: 100%; }}
.bar-cell {{ min-width: 140px; }}
.bar-track {{ height: 10px; border-radius: 5px; background: var(--line); overflow: hidden; }}
.bar-track span {{ display: block; height: 100%; background: var(--accent); }}
.status-pill {{ display: inline-block; color: #fff; border-radius: 999px; padding: 2px 9px; font-size: 12px; }}
.success-banner {{ background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; border-radius: 8px; padding: 14px 16px; }}
.exception-table th:first-child, .exception-table td:first-child {{ min-width: 190px; }}
.reason-cell {{ max-width: 360px; overflow-wrap: anywhere; }}
@media (max-width: 900px) {{
  .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .grid-2 {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 560px) {{
  body {{ padding: 20px 12px; }}
  .metrics {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
  .metric-card:first-child {{ grid-column: span 2; }}
  .metric-value {{ font-size: 24px; }}
  table {{ font-size: 12px; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{_escape(title)}</h1>
    <div class="muted">生成时间：{_escape(generated_at)}<br>结果文件目录：{_escape(str(input_dir))}</div>
  </header>

  <section class="metrics">
    {summary_cards}
  </section>

  <section class="grid-2">
    <div class="panel">
      <h2>总体状态</h2>
      <div class="donut-box">
        <div class="donut-wrap">
          <div class="donut-center">
            <div><strong>{metrics['total']}</strong><div class="muted">用例</div></div>
          </div>
          {_donut(counts)}
        </div>
      </div>
      {_legend()}
    </div>
    <div class="panel">
      <h2>状态汇总</h2>
      {_stacked_bar(counts)}
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:16px">
        {_summary_card("失败数", counts['failed'], " 个", STATUS_COLORS['failed'])}
        {_summary_card("异常数", counts['error'], " 个", STATUS_COLORS['error'])}
        {_summary_card("跳过数", counts['skipped'], " 个", STATUS_COLORS['skipped'])}
        {_summary_card("P50 耗时", _fmt_ms(metrics['p50_ms']), "", "#2563eb")}
      </div>
    </div>
  </section>

  <section class="panel">
    {interface_table}
  </section>

  <section class="panel">
    {_response_time_rows(aggregated['records'])}
  </section>

  <section class="grid-2">
    <div class="panel">{category_table}</div>
    <div class="panel">{priority_table}</div>
  </section>

  <section class="panel">
    <h2>异常用例情况</h2>
    {_exception_table(aggregated['exceptions'])}
  </section>

  {load_error_html}
  {file_errors_html}
</div>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")
    manifest_path = output_path.parent / "visualization_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "report": str(output_path),
                "metrics": metrics,
                "counts": counts,
                "source_dir": str(input_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output_path


def build_parser() -> argparse.ArgumentParser:
    """创建测试结果可视化工具的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="从 API 测试结果 JSON 生成自包含 HTML 报告")
    parser.add_argument("--input", required=True, type=Path, help="包含 *_results.json 的目录")
    parser.add_argument("--output", type=Path, default=None, help="HTML 报告输出路径，默认写入 input 目录")
    parser.add_argument("--title", default="API 接口测试报告", help="报告标题")
    return parser


def main() -> int:
    """读取命令行参数、生成 HTML 报告并打印输出路径。"""
    args = build_parser().parse_args()
    output = build_html_report(args.input, args.output, title=args.title)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
