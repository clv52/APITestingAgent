"""Export declarative API test-case JSON into a human-readable Excel workbook."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILDER = Path(__file__).with_suffix(".mjs")


class ExcelExportError(RuntimeError):
    """Raised when the spreadsheet builder cannot create a workbook."""


def export_case_workbook(
    case_path: Path,
    output_path: Path,
    *,
    results_path: Path | None = None,
    preview_path: Path | None = None,
) -> Path:
    """调用 Node 工作簿构建器，并用临时文件保证最终 xlsx 不会半写入。"""
    case_path = case_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    node = os.environ.get("API_TEST_EXCEL_NODE") or shutil.which("node")
    if not node:
        raise ExcelExportError("未找到 Node.js；请安装 Node.js 或设置 API_TEST_EXCEL_NODE")
    # Node 先写随机临时文件，成功后 replace；Excel 被占用时保留原文件不变。
    temporary_output = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.tmp.xlsx")
    command = [str(node), str(BUILDER), "--input", str(case_path), "--output", str(temporary_output)]
    if results_path is not None:
        results_path = results_path.expanduser().resolve()
        if not results_path.is_file():
            raise ExcelExportError(f"测试结果文件不存在：{results_path}")
        command.extend(["--results", str(results_path)])
    if preview_path is not None:
        command.extend(["--preview", str(preview_path.expanduser().resolve())])
    try:
        result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode != 0 or not temporary_output.is_file():
            detail = (result.stderr or result.stdout or "未知错误").strip()
            raise ExcelExportError(f"Excel 生成失败：{detail}")
        try:
            temporary_output.replace(output_path)
        except PermissionError as error:
            raise ExcelExportError(f"Excel 文件正被占用，请关闭后重试：{output_path}") from error
    finally:
        temporary_output.unlink(missing_ok=True)
    # artifact-tool may emit an internal inspection sidecar next to the workbook;
    # it is a build diagnostic rather than a user-facing task artifact.
    temporary_output.with_suffix(temporary_output.suffix + ".inspect.ndjson").unlink(missing_ok=True)
    output_path.with_suffix(output_path.suffix + ".inspect.ndjson").unlink(missing_ok=True)
    return output_path


def update_case_workbook_results(case_path: Path, results_path: Path, interfaces_dir: Path) -> Path:
    """Regenerate the interface workbook with execution results mapped by case id."""

    case_path = case_path.expanduser().resolve()
    interfaces_dir = interfaces_dir.expanduser().resolve()
    prefix = case_path.name[:3]
    markdown_files = sorted(interfaces_dir.glob(f"{prefix}_*.md"))
    if not markdown_files:
        raise ExcelExportError(f"没有找到用例 {case_path.name} 对应的接口 Markdown")
    markdown_path = markdown_files[0]
    output_path = markdown_path.with_name(f"{markdown_path.stem}_测试用例.xlsx")
    return export_case_workbook(case_path, output_path, results_path=results_path)


def main() -> int:
    """运行 Excel 导出命令，并将生成文件路径打印到标准输出。"""
    parser = argparse.ArgumentParser(description="将接口测试用例 JSON 导出为可审阅的 Excel。")
    parser.add_argument("--input", required=True, type=Path, help="单接口 *_cases.json")
    parser.add_argument("--output", required=True, type=Path, help="输出 .xlsx")
    parser.add_argument("--results", type=Path, help="可选的单接口 *_results.json；提供后回填执行结果")
    parser.add_argument("--preview", type=Path, help="可选的 PNG 预览图")
    args = parser.parse_args()
    path = export_case_workbook(args.input, args.output, results_path=args.results, preview_path=args.preview)
    print(path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExcelExportError as error:
        print(f"ERROR: {error}")
        raise SystemExit(2) from error
