"""Parse a PDF with MinerU and expose stable artifacts for the API-test skill."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


def _default_mineru_path() -> Path:
    """推导当前 Python 环境中 MinerU 命令行程序的默认路径。"""
    return Path(sys.executable).resolve().parent / "Scripts" / "mineru.exe"


def _find_artifacts(pdf_path: Path, output_dir: Path) -> dict[str, Path | None]:
    """把 MinerU 不固定的嵌套输出，收敛为后续阶段需要的三个稳定路径。"""
    pdf_name = pdf_path.stem
    markdown_candidates = sorted(
        path for path in output_dir.rglob(f"{pdf_name}.md") if path.is_file()
    )
    if not markdown_candidates:
        raise FileNotFoundError(f"没有找到 MinerU 生成的 Markdown：{pdf_name}.md")
    markdown_path = markdown_candidates[0]
    preferred_content_list = markdown_path.with_name(f"{pdf_name}_content_list.json")
    content_list_path = preferred_content_list if preferred_content_list.is_file() else None
    if content_list_path is None:
        candidates = sorted(
            path for path in markdown_path.parent.glob("*_content_list.json") if path.is_file()
        )
        content_list_path = candidates[0] if candidates else None
    # images 必须与 Markdown 同级；后续复制时也保持这一层级，不能只复制 md。
    images_dir = markdown_path.parent / "images"
    return {
        "markdown": markdown_path,
        "content_list": content_list_path,
        "images_dir": images_dir if images_dir.is_dir() else None,
    }


def parse_pdf_to_markdown(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    mineru_path: str | Path | None = None,
    mode: str = "auto",
) -> str:
    """Run MinerU and return the generated Markdown text.

    The generated Markdown is not moved or rewritten, so its `images/...`
    relative links continue to resolve against MinerU's sibling images folder.
    """
    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"输入必须是存在的 PDF：{pdf_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = Path(mineru_path).expanduser().resolve() if mineru_path else _default_mineru_path()
    if not executable.is_file():
        raise FileNotFoundError(
            f"找不到 MinerU 可执行文件：{executable}。可通过 --mineru 指定 mineru.exe。"
        )
    # 外部进程只负责 OCR/版面解析；产物定位和清单生成仍由本文件控制。
    command = [str(executable), "-p", str(pdf_path), "-o", str(output_dir), "-m", mode]
    LOGGER.info("开始解析 PDF：%s", pdf_path)
    subprocess.run(command, check=True)
    artifacts = _find_artifacts(pdf_path, output_dir)
    markdown_path = artifacts["markdown"]
    assert isinstance(markdown_path, Path)
    LOGGER.info("解析完成：%s", markdown_path)
    return markdown_path.read_text(encoding="utf-8-sig")


def parse_pdf(
    pdf_path: Path,
    output_dir: Path,
    *,
    mineru_path: Path | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """运行解析并生成稳定清单；Agent 后续只依赖清单，不猜 MinerU 目录。"""
    parse_pdf_to_markdown(pdf_path, output_dir, mineru_path=mineru_path, mode=mode)
    artifacts = _find_artifacts(pdf_path.resolve(), output_dir.resolve())
    markdown_path = artifacts["markdown"]
    assert isinstance(markdown_path, Path)
    manifest = {
        "pdf": str(pdf_path.resolve()),
        "markdown": str(markdown_path),
        "content_list": str(artifacts["content_list"]) if artifacts["content_list"] else None,
        "images_dir": str(artifacts["images_dir"]) if artifacts["images_dir"] else None,
        "image_links_are_relative_to_markdown": True,
    }
    manifest_path = markdown_path.parent / "parse_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest"] = str(manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    """创建 PDF 解析工具的命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="使用 MinerU 解析 PDF，并输出 Markdown/content_list/images 的稳定产物。")
    parser.add_argument("--input", required=True, type=Path, help="输入 PDF")
    parser.add_argument("--output", required=True, type=Path, help="MinerU 输出根目录")
    parser.add_argument("--mineru", type=Path, default=None, help="可选：指定 mineru.exe 路径")
    parser.add_argument("--mode", default="auto", choices=["auto", "ocr", "txt"], help="MinerU 解析模式")
    parser.add_argument("--verbose", action="store_true", help="显示调试日志")
    return parser


def main() -> int:
    """执行 PDF 解析命令并以 JSON 形式输出产物清单。"""
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    manifest = parse_pdf(args.input, args.output, mineru_path=args.mineru, mode=args.mode)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
