import subprocess
import sys
from pathlib import Path
from loguru import logger


def parse_pdf_to_markdown(pdf_path: str, output_dir: str) -> str:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 当前 conda 环境中的 mineru.exe
    mineru_path = Path(sys.executable).parent / "Scripts" / "mineru.exe"

    if not mineru_path.exists():
        raise FileNotFoundError(f"找不到 MinerU：{mineru_path}")

    cmd = [
        str(mineru_path),
        "-p", str(pdf_path),
        "-o", str(output_dir),
        "-m", "auto",
    ]

    logger.info(f"开始解析 PDF: {pdf_path}")

    subprocess.run(cmd, check=True)

    pdf_name = pdf_path.stem

    md_files = list(output_dir.rglob(f"{pdf_name}.md"))

    if not md_files:
        raise FileNotFoundError("没有找到 MinerU 生成的 Markdown 文件")

    md_path = md_files[0]
    md_content = md_path.read_text(encoding="utf-8")

    logger.info(f"解析完成！Markdown: {md_path}")

    return md_content


if __name__ == "__main__":
    md = parse_pdf_to_markdown(
        "../asset/华为云OrgID_API接口说明文档_中文版.pdf",
        "../output",
    )

    print(md[:2000])