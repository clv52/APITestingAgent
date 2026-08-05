from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openai import OpenAI


START_SYSTEM_PROMPT = """
你是一个严谨的 API 接口文档切割器。

输入是一段按原始文档顺序排列的内容。每一行都有稳定的全局行号，例如 [L0000123]。
内容中可能包含普通文字、表格行、图片占位、目录、代码示例和公共说明。

你的任务是识别所有正在被文档正式定义的独立 HTTP API 接口，并返回每个接口的最早开始行。

判断规则：
1. 接口通常具有接口名称、HTTP 方法、请求路径或完整 URL。
2. 接口开始行应是属于该接口的最早一行，通常是接口标题；不要只返回 POST/GET 所在行。
3. 目录中的接口标题不是接口正文。
4. curl、Python、Java、JavaScript 等请求示例中的 METHOD 和 URL 不能单独视为新接口。
5. 同一接口内重复出现相同 METHOD + PATH，仍然只算一个接口。
6. 表格行和图片占位属于其相邻正文，不能单独视为接口。
7. 不得猜测不存在的信息。
8. 只返回 JSON，不要返回解释、Markdown 或代码围栏。

输出格式：
{
  "interfaces": [
    {
      "name": "接口名称",
      "method": "GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS或空字符串",
      "path": "请求路径或完整URL，无法确定时为空字符串",
      "start_line": "L0000001",
      "confidence": 0.0
    }
  ]
}
""".strip()


BOUNDARY_SYSTEM_PROMPT = """
你是一个严谨的 API 接口边界确认器。

输入包含一个目标接口的候选文本范围，每行都有稳定的全局行号。
你需要确定这个目标接口在候选范围内的准确开始行和结束行。

边界规则：
1. 开始行应包括接口标题、位于方法和路径之前且专属于当前接口的功能说明。
2. 结束行应尽可能包括当前接口的请求参数、请求示例、响应参数、响应示例、状态码、错误码和注意事项。
3. 不得包含下一个接口的标题或内容。
4. 不得把文档末尾的公共错误码、公共鉴权、附录、修订记录等全局章节错误归入当前接口。
5. 请求示例中的 curl、Python、Java 等 METHOD + PATH 不是新接口边界。
6. 表格开始、表格行、表格结束应当作为整体保留；不能把边界切在表格中间。
7. 图片占位应归入它紧邻的接口内容。
8. 只返回原文行号，不要改写原文。
9. 只返回 JSON，不要返回解释、Markdown 或代码围栏。

输出格式：
{
  "name": "接口名称",
  "method": "HTTP方法或空字符串",
  "path": "请求路径或完整URL或空字符串",
  "start_line": "L0000001",
  "end_line": "L0000100",
  "confidence": 0.0
}
""".strip()


LINE_ID_RE = re.compile(r"^L(\d{7})$")
HTTP_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", re.I)


def line_number(line_id: str) -> int:
    match = LINE_ID_RE.fullmatch(line_id)
    if not match:
        raise ValueError(f"非法行号：{line_id}")
    return int(match.group(1))


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("模型返回的顶层结构不是JSON对象")
        return value
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"模型响应中没有找到JSON对象：{text[:500]}")
        value = json.loads(text[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("模型返回的顶层结构不是JSON对象")
        return value


def create_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def call_deepseek_json(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    retries: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=False,
                reasoning_effort="high",
                extra_body={"thinking": {"type": "enabled"}},
            )
            content = response.choices[0].message.content or ""
            return extract_json_object(content)
        except Exception as exc:
            last_error = exc
            if attempt == retries:
                break
            sleep_seconds = min(2 ** attempt, 8)
            print(f"[警告] 第 {attempt} 次调用失败：{exc}，{sleep_seconds}s 后重试")
            time.sleep(sleep_seconds)

    raise RuntimeError(f"DeepSeek调用失败：{last_error}")


def render_lines(llm_lines: list[dict[str, Any]], start: int, end: int) -> str:
    selected = llm_lines[start:end]
    return "\n".join(f"[{item['line_id']}] {item['text']}" for item in selected)


def normalize_path(path: str) -> str:
    path = path.strip().rstrip("，。；;)")
    if path.startswith(("http://", "https://")):
        parsed = urlsplit(path)
        normalized = parsed.path or "/"
        if parsed.query:
            normalized += "?" + parsed.query
        return normalized
    return path


def detect_interface_starts(
    client: OpenAI,
    model: str,
    llm_lines: list[dict[str, Any]],
    window_size: int,
    overlap: int,
) -> list[dict[str, Any]]:
    if overlap >= window_size:
        raise ValueError("overlap必须小于window_size")

    candidates: list[dict[str, Any]] = []
    step = window_size - overlap

    for start in range(0, len(llm_lines), step):
        end = min(start + window_size, len(llm_lines))
        window_text = render_lines(llm_lines, start, end)

        user_prompt = f"""
下面是接口文档的一段窗口文本。请识别其中所有正式定义的独立接口，并返回接口最早开始行。
窗口范围：{llm_lines[start]['line_id']} 到 {llm_lines[end - 1]['line_id']}

文档内容：
{window_text}
""".strip()

        result = call_deepseek_json(
            client=client,
            model=model,
            system_prompt=START_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        for item in result.get("interfaces", []):
            if not isinstance(item, dict):
                continue
            start_line = str(item.get("start_line", "")).strip()
            if not LINE_ID_RE.fullmatch(start_line):
                continue
            candidates.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "method": str(item.get("method", "")).upper().strip(),
                    "path": str(item.get("path", "")).strip(),
                    "start_line": start_line,
                    "confidence": float(item.get("confidence", 0.0) or 0.0),
                }
            )

        if end == len(llm_lines):
            break

    # 先按完全相同起始行去重
    by_start: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = item["start_line"]
        current = by_start.get(key)
        if current is None or item["confidence"] > current["confidence"]:
            by_start[key] = item

    ordered = sorted(by_start.values(), key=lambda x: line_number(x["start_line"]))

    # 再对相邻且 method+path 相同的重复项去重
    deduped: list[dict[str, Any]] = []
    for item in ordered:
        item_path = normalize_path(item["path"])
        duplicate_index: int | None = None
        for index, existing in enumerate(deduped):
            same_signature = (
                item["method"]
                and item_path
                and item["method"] == existing["method"]
                and item_path == normalize_path(existing["path"])
            )
            close_start = abs(line_number(item["start_line"]) - line_number(existing["start_line"])) <= overlap
            if same_signature and close_start:
                duplicate_index = index
                break

        if duplicate_index is None:
            deduped.append(item)
        elif item["confidence"] > deduped[duplicate_index]["confidence"]:
            deduped[duplicate_index] = item

    return sorted(deduped, key=lambda x: line_number(x["start_line"]))


def compact_boundary_context(
    llm_lines: list[dict[str, Any]],
    start: int,
    end: int,
    max_chars: int,
) -> str:
    full = render_lines(llm_lines, start, end)
    if len(full) <= max_chars:
        return full

    # 边界判断主要依赖候选范围头尾；过长时保留头尾并明确省略区间
    head_count = min(250, max(1, (end - start) // 2))
    tail_count = min(250, max(1, (end - start) // 2))
    head = render_lines(llm_lines, start, min(start + head_count, end))
    tail_start = max(start + head_count, end - tail_count)
    tail = render_lines(llm_lines, tail_start, end)
    return head + "\n[中间内容因长度限制省略，但行号连续存在]\n" + tail


def refine_boundaries(
    client: OpenAI,
    model: str,
    llm_lines: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context_before: int = 30,
    max_context_chars: int = 120_000,
) -> list[dict[str, Any]]:
    if not anchors:
        return []

    line_to_index = {item["line_id"]: index for index, item in enumerate(llm_lines)}
    boundaries: list[dict[str, Any]] = []

    for index, anchor in enumerate(anchors):
        anchor_index = line_to_index[anchor["start_line"]]
        candidate_start = max(0, anchor_index - context_before)

        if index + 1 < len(anchors):
            next_start_index = line_to_index[anchors[index + 1]["start_line"]]
            candidate_end = next_start_index
        else:
            candidate_end = len(llm_lines)

        context = compact_boundary_context(
            llm_lines=llm_lines,
            start=candidate_start,
            end=candidate_end,
            max_chars=max_context_chars,
        )

        user_prompt = f"""
目标接口候选信息：
名称：{anchor['name']}
方法：{anchor['method']}
路径：{anchor['path']}
初步开始行：{anchor['start_line']}

请在下面候选范围中确认该接口的准确开始行和结束行。
候选范围：{llm_lines[candidate_start]['line_id']} 到 {llm_lines[candidate_end - 1]['line_id']}

候选文本：
{context}
""".strip()

        result = call_deepseek_json(
            client=client,
            model=model,
            system_prompt=BOUNDARY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        start_line = str(result.get("start_line", anchor["start_line"])).strip()
        end_line = str(result.get("end_line", llm_lines[candidate_end - 1]["line_id"])).strip()

        if start_line not in line_to_index:
            start_line = anchor["start_line"]
        if end_line not in line_to_index:
            end_line = llm_lines[candidate_end - 1]["line_id"]

        start_idx = line_to_index[start_line]
        end_idx = line_to_index[end_line]
        if start_idx > end_idx:
            start_line = anchor["start_line"]
            end_line = llm_lines[candidate_end - 1]["line_id"]

        boundaries.append(
            {
                "name": str(result.get("name", anchor["name"])).strip() or anchor["name"],
                "method": str(result.get("method", anchor["method"])).upper().strip() or anchor["method"],
                "path": str(result.get("path", anchor["path"])).strip() or anchor["path"],
                "start_line": start_line,
                "end_line": end_line,
                "confidence": float(result.get("confidence", anchor["confidence"]) or 0.0),
            }
        )

    # 按开始行排序并消除交叉区间
    boundaries.sort(key=lambda x: line_number(x["start_line"]))
    for index in range(len(boundaries) - 1):
        current = boundaries[index]
        next_item = boundaries[index + 1]
        if line_number(current["end_line"]) >= line_number(next_item["start_line"]):
            corrected_end_number = line_number(next_item["start_line"]) - 1
            if corrected_end_number >= line_number(current["start_line"]):
                current["end_line"] = f"L{corrected_end_number:07d}"

    return boundaries


def block_overlaps(block: dict[str, Any], start_num: int, end_num: int) -> bool:
    block_start = line_number(block["line_start"])
    block_end = line_number(block["line_end"])
    return not (block_end < start_num or block_start > end_num)


def markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def escape(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")

    header = padded[0]
    body = padded[1:]
    lines = [
        "| " + " | ".join(escape(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")
    return cleaned[:80] or fallback


def write_clean_interfaces(
    document: dict[str, Any],
    boundaries: list[dict[str, Any]],
    output_json: Path,
    output_markdown: Path,
    individual_dir: Path | None = None,
) -> list[dict[str, Any]]:
    blocks = document["blocks"]
    clean_interfaces: list[dict[str, Any]] = []
    markdown_sections: list[str] = []

    if individual_dir is not None:
        individual_dir.mkdir(parents=True, exist_ok=True)

    for interface_index, boundary in enumerate(boundaries, start=1):
        start_num = line_number(boundary["start_line"])
        end_num = line_number(boundary["end_line"])
        selected_blocks = [block for block in blocks if block_overlaps(block, start_num, end_num)]

        content_blocks: list[dict[str, Any]] = []
        md_lines: list[str] = [f"# {boundary['name'] or f'接口{interface_index}'}", ""]

        if boundary.get("method") or boundary.get("path"):
            md_lines.append(
                f"**请求：** `{boundary.get('method', '').strip()} {boundary.get('path', '').strip()}`".rstrip()
            )
            md_lines.append("")

        for block in selected_blocks:
            block_type = block["type"]

            if block_type == "text":
                content = block["content"]
                content_blocks.append({"type": "text", "content": content})
                md_lines.append(content)
                md_lines.append("")

            elif block_type == "table":
                rows = block.get("rows", [])
                content_blocks.append({"type": "table", "rows": rows})
                table_md = markdown_table(rows)
                if table_md:
                    md_lines.append(table_md)
                    md_lines.append("")

            elif block_type == "image":
                source_path = Path(block["image_path"])
                try:
                    relative_path = os.path.relpath(source_path, output_markdown.parent)
                except ValueError:
                    relative_path = str(source_path)
                relative_path = relative_path.replace(os.sep, "/")
                content_blocks.append({"type": "image", "image_path": str(source_path)})
                md_lines.append(f"![接口文档图片]({relative_path})")
                md_lines.append("")

        clean_item = {
            "name": boundary.get("name", ""),
            "method": boundary.get("method", ""),
            "path": boundary.get("path", ""),
            "content_blocks": content_blocks,
        }
        clean_interfaces.append(clean_item)

        section_text = "\n".join(md_lines).strip() + "\n"
        markdown_sections.append(section_text)

        if individual_dir is not None:
            filename = safe_filename(
                boundary.get("name", ""),
                f"interface_{interface_index:03d}",
            )
            (individual_dir / f"{interface_index:03d}_{filename}.md").write_text(
                section_text,
                encoding="utf-8",
            )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)

    output_json.write_text(
        json.dumps(clean_interfaces, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_markdown.write_text(
        "\n---\n\n".join(markdown_sections),
        encoding="utf-8",
    )

    return clean_interfaces


def main() -> None:
    parser = argparse.ArgumentParser(description="使用DeepSeek识别接口边界并重新封装接口内容")
    parser.add_argument(
        "input_json",
        type=Path,
        help="第一步生成的 document_content.json",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("interface_split"))
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--window-size", type=int, default=350)
    parser.add_argument("--overlap", type=int, default=80)
    args = parser.parse_args()

    document = json.loads(args.input_json.read_text(encoding="utf-8"))
    llm_lines = document.get("llm_lines", [])
    if not llm_lines:
        raise RuntimeError("输入文件中没有 llm_lines")

    client = create_client()

    print("第1阶段：识别接口开始位置……")
    anchors = detect_interface_starts(
        client=client,
        model=args.model,
        llm_lines=llm_lines,
        window_size=args.window_size,
        overlap=args.overlap,
    )
    if not anchors:
        raise RuntimeError("模型没有识别到接口")

    print(f"识别到 {len(anchors)} 个接口候选，开始确认完整边界……")
    boundaries = refine_boundaries(
        client=client,
        model=args.model,
        llm_lines=llm_lines,
        anchors=anchors,
    )

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    boundary_path = output_dir / "interface_boundaries.json"
    boundary_path.write_text(
        json.dumps(boundaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    write_clean_interfaces(
        document=document,
        boundaries=boundaries,
        output_json=output_dir / "interfaces_clean.json",
        output_markdown=output_dir / "interfaces.md",
        individual_dir=output_dir / "interfaces",
    )

    print(f"接口边界：{boundary_path}")
    print(f"清洗后接口JSON：{output_dir / 'interfaces_clean.json'}")
    print(f"合并排版文件：{output_dir / 'interfaces.md'}")
    print(f"单接口文件目录：{output_dir / 'interfaces'}")


if __name__ == "__main__":
    main()
