"""把 MinerU 的结构化 content_list 重新渲染为可读 Markdown。

学习时先看 render_item() 的类型分发，再看 content_list_to_markdown() 的
顺序拼接；各 render_xxx() 只是不同 block 类型的局部规则。
"""

import argparse
import json
from pathlib import Path
from typing import Any


# MinerU legacy content_list 中不会输出到 Markdown 的页面噪声类型。
SKIP_TYPES = {
    "header",
    "footer",
    "page_number",
}


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件。"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_content_list(data: Any) -> list[dict]:
    """
    从 JSON 数据中取得 content_list。

    支持两种输入：

    1. MinerU 原始 content_list.json
       [
           {...},
           {...}
       ]

    2. 前面接口切割脚本生成的接口 JSON
       {
           "metadata": {...},
           "content_list": [
               {...},
               {...}
           ]
       }
    """
    if isinstance(data, list):
        content_list = data

    elif isinstance(data, dict) and isinstance(data.get("content_list"), list):
        content_list = data["content_list"]

    else:
        raise ValueError(
            "不支持的 JSON 结构：顶层必须是 list，"
            "或者 dict 中包含 list 类型的 content_list 字段。"
        )

    for index, item in enumerate(content_list):
        if not isinstance(item, dict):
            raise ValueError(
                f"content_list[{index}] 不是 JSON object：{type(item).__name__}"
            )

    return content_list


def normalize_lines(value: Any) -> list[str]:
    """
    将 caption / footnote 等字段规范成字符串列表。

    MinerU legacy content_list 通常已经是 list[str]，
    这里做少量兼容，避免异常输入导致程序崩溃。
    """
    if value is None:
        return []

    if isinstance(value, list):
        return [
            str(item)
            for item in value
            if item is not None and str(item) != ""
        ]

    if isinstance(value, str):
        return [value] if value != "" else []

    return [str(value)]


def render_text(item: dict) -> str | None:
    """
    渲染 MinerU text block。

    对照 MinerU 3.4.4 原始 Markdown：
    - 有 text_level：转换为 Markdown 标题
    - 无 text_level：直接输出正文
    - text 为空但 text_level 存在时，仍输出对应数量的 '#'

    例如：
        text_level = 2, text = "4.1.1 获取用户"
        -> "## 4.1.1 获取用户"

        text_level = 2, text = ""
        -> "##"
    """
    text = item.get("text", "")
    if text is None:
        text = ""
    else:
        text = str(text)

    text_level = item.get("text_level")

    if text_level is not None:
        try:
            level = int(text_level)
        except (TypeError, ValueError):
            level = 1

        # Markdown 标准标题最多 6 级。
        level = max(1, min(level, 6))
        prefix = "#" * level

        if text:
            return f"{prefix} {text}"

        return prefix

    if text:
        return text

    return None


def render_table(item: dict) -> str | None:
    """
    渲染 MinerU table block。

    MinerU 3.4.4 legacy Markdown 的顺序为：

        table_caption

        table_body

        table_footnote

    各部分之间使用一个 Markdown 空段，即 '\\n\\n'。

    table_body 已经是 MinerU 生成的 HTML table，
    因此不重新解析、不重新格式化，直接保留原文。
    """
    parts: list[str] = []

    captions = normalize_lines(item.get("table_caption"))
    footnotes = normalize_lines(item.get("table_footnote"))

    table_body = item.get("table_body", "")
    if table_body is None:
        table_body = ""
    else:
        table_body = str(table_body)

    parts.extend(captions)

    if table_body:
        parts.append(table_body)

    parts.extend(footnotes)

    if not parts:
        return None

    return "\n\n".join(parts)


def render_image(item: dict) -> str | None:
    """
    渲染 MinerU image block。

    对照 MinerU 3.4.4 原始 Markdown，图片标题与图片之间使用：

        "  \\n"

    即 Markdown 的 hard line break。

    例如：

        图 3-1 URI 示意图··
        ![](images/xxx.jpg)

    其中“··”代表两个空格。

    默认完全保留 img_path，不重写路径，
    从而与 MinerU 原始 Markdown 保持一致。
    """
    lines: list[str] = []

    captions = normalize_lines(item.get("image_caption"))
    footnotes = normalize_lines(item.get("image_footnote"))

    img_path = item.get("img_path", "")
    if img_path is None:
        img_path = ""
    else:
        img_path = str(img_path)

    lines.extend(captions)

    if img_path:
        lines.append(f"![]({img_path})")

    lines.extend(footnotes)

    if not lines:
        return None

    return "  \n".join(lines)


def render_code(item: dict) -> str | None:
    """
    渲染 MinerU code block。

    code_body 在 content_list 中通常已经包含完整 fenced code block：

        ```json
        {...}
        ```

    因此直接保留，不重新包裹 ```。
    """
    parts: list[str] = []

    captions = normalize_lines(item.get("code_caption"))

    code_body = item.get("code_body", "")
    if code_body is None:
        code_body = ""
    else:
        code_body = str(code_body)

    parts.extend(captions)

    if code_body:
        parts.append(code_body)

    if not parts:
        return None

    return "\n\n".join(parts)


def render_equation(item: dict) -> str | None:
    """
    对 legacy content_list 中可能出现的公式块做兼容。

    不同 MinerU backend / 版本的公式字段可能略有差异，
    因此依次尝试 text / content / equation。

    如果内容本身已经带 '$$'，则原样保留；
    否则使用 display math 包裹。

    注意：
    你当前上传的 OrgID 文档中没有 equation block，
    这个分支属于兼容逻辑，不参与该样本的 1:1 验证。
    """
    value = (
        item.get("text")
        or item.get("content")
        or item.get("equation")
        or ""
    )

    if not value:
        return None

    value = str(value)

    stripped = value.strip()

    if stripped.startswith("$$") and stripped.endswith("$$"):
        return value

    return f"$$\n{value}\n$$"


def render_generic(item: dict) -> str | None:
    """
    未知 block 类型的保守兜底。

    优先保留已有 text / content，
    不凭空改变文本。
    """
    value = item.get("text")

    if value is None or value == "":
        value = item.get("content")

    if value is None or value == "":
        return None

    return str(value)


def render_item(item: dict, strict: bool = False) -> str | None:
    """将单个 MinerU content_list item 渲染成 Markdown block。"""
    item_type = item.get("type")

    # 页面页眉、页脚等噪声在这里统一丢弃，避免每个渲染器重复判断。
    if item_type in SKIP_TYPES:
        return None

    if item_type == "text":
        return render_text(item)

    if item_type == "table":
        return render_table(item)

    if item_type == "image":
        return render_image(item)

    if item_type == "code":
        return render_code(item)

    if item_type in {"equation", "interline_equation"}:
        return render_equation(item)

    # strict 用于发现新 MinerU 类型；普通模式则尽量保留未知 block 的文本。
    if strict:
        raise ValueError(f"遇到未支持的 content_list type：{item_type!r}")

    return render_generic(item)


def content_list_to_markdown(
    content_list: list[dict],
    strict: bool = False,
) -> str:
    """
    将 MinerU legacy content_list 转为 Markdown。

    MinerU 3.4.4 原始 Markdown 的 block 之间使用 '\\n\\n'。
    这里不额外 strip 每个 block，以尽量避免改变 MinerU 原始内容。
    """
    # 不在这里重排 block：接口切分依赖原 content_list 的先后顺序。
    blocks: list[str] = []

    for item in content_list:
        block = render_item(item, strict=strict)

        if block is not None:
            blocks.append(block)

    return "\n\n".join(blocks)


def convert_json_file(
    input_path: Path,
    output_path: Path,
    strict: bool = False,
) -> None:
    """转换一个 JSON 文件。"""
    data = load_json(input_path)
    content_list = extract_content_list(data)

    markdown = content_list_to_markdown(
        content_list,
        strict=strict,
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        markdown,
        encoding="utf-8",
    )

    print(
        f"[OK] {input_path} "
        f"-> {output_path} "
        f"(items={len(content_list)}, chars={len(markdown)})"
    )


def convert_directory(
    input_dir: Path,
    output_dir: Path,
    recursive: bool = False,
    strict: bool = False,
) -> None:
    """
    批量转换目录中的 JSON。

    会自动跳过：
    - api_boundaries.json 等不含 content_list 的 dict JSON
    - 不是 list / {content_list: [...]} 格式的 JSON

    这样可以直接指向 llm_split_interfaces.py 输出的 interfaces 目录。
    """
    pattern = "**/*.json" if recursive else "*.json"

    json_files = sorted(
        path
        for path in input_dir.glob(pattern)
        if path.is_file()
    )

    if not json_files:
        raise FileNotFoundError(
            f"目录中没有找到 JSON 文件：{input_dir}"
        )

    converted = 0
    skipped = 0

    for input_path in json_files:
        relative = input_path.relative_to(input_dir)
        output_path = (output_dir / relative).with_suffix(".md")

        try:
            convert_json_file(
                input_path=input_path,
                output_path=output_path,
                strict=strict,
            )
            converted += 1

        except ValueError as error:
            print(
                f"[SKIP] {input_path}: {error}"
            )
            skipped += 1

    print()
    print(
        f"[DONE] converted={converted}, skipped={skipped}, "
        f"output={output_dir}"
    )


def build_parser() -> argparse.ArgumentParser:
    """创建 content_list 转 Markdown 命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description=(
            "将 MinerU 3.4.4 legacy content_list JSON "
            "转换为尽可能接近 MinerU 原始输出的 Markdown。"
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        help=(
            "输入 JSON 文件或目录。"
            "支持原始 content_list.json，"
            "以及 {metadata, content_list} 接口切片 JSON。"
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help=(
            "输出 .md 文件或输出目录。"
            "文件输入未指定时默认写到输入文件旁边；"
            "目录输入未指定时默认写到 <input>/markdown。"
        ),
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="目录模式下递归处理子目录中的 JSON。",
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="遇到未知 block type 时直接报错，而不是保守兜底。",
    )

    return parser


def main() -> None:
    """解析命令行参数并执行单文件或目录批量转换。"""
    parser = build_parser()
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()

    if not input_path.exists():
        raise FileNotFoundError(
            f"输入路径不存在：{input_path}"
        )

    if input_path.is_file():
        if input_path.suffix.lower() != ".json":
            raise ValueError(
                f"文件输入必须是 .json：{input_path}"
            )

        if args.output:
            output_path = Path(args.output).expanduser().resolve()

            # 如果用户传入已有目录，则自动使用输入文件名。
            if output_path.exists() and output_path.is_dir():
                output_path = output_path / f"{input_path.stem}.md"

            # 没有 .md 后缀时仍视为用户指定的文件路径，
            # 但自动补上 .md 更符合命令行直觉。
            elif output_path.suffix.lower() != ".md":
                output_path = output_path.with_suffix(".md")

        else:
            output_path = input_path.with_suffix(".md")

        convert_json_file(
            input_path=input_path,
            output_path=output_path,
            strict=args.strict,
        )

        return

    if input_path.is_dir():
        if args.output:
            output_dir = Path(args.output).expanduser().resolve()
        else:
            output_dir = input_path / "markdown"

        convert_directory(
            input_dir=input_path,
            output_dir=output_dir,
            recursive=args.recursive,
            strict=args.strict,
        )

        return

    raise ValueError(
        f"既不是普通文件也不是目录：{input_path}"
    )


if __name__ == "__main__":
    main()
