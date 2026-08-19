"""使用 LLM 识别 content_list 中的接口语义边界，再由本地代码切片落盘。

模型只返回 start_idx/end_idx；validate_boundaries() 和 verify_api_content()
负责把模型判断约束在原始 content_list 范围内。
"""

import os
import re
import json
import time
import argparse
from pathlib import Path
from typing import Any

from api_test_case_spec import load_env_file
from content_list_to_md import content_list_to_markdown


# ============================================================
# 加载 .env
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

ENV_VALUES = load_env_file(ENV_PATH)


# ============================================================
# 从 .env 读取 DeepSeek 配置
# ============================================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or ENV_VALUES.get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL") or ENV_VALUES.get("DEEPSEEK_BASE_URL")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL") or ENV_VALUES.get("DEEPSEEK_MODEL")


# ============================================================
# 其他配置
# ============================================================

# 单个普通文本元素最多给 LLM 展示多少字符
MAX_ITEM_PREVIEW_CHARS = 1200

# API 调用最大重试次数
MAX_RETRY = 3


# ============================================================
# 检查环境变量
# ============================================================

def check_env():
    """
    检查 DeepSeek 所需环境变量是否完整。
    """

    required_env = {
        "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
        "DEEPSEEK_BASE_URL": DEEPSEEK_BASE_URL,
        "DEEPSEEK_MODEL": DEEPSEEK_MODEL,
    }

    missing = [
        key
        for key, value in required_env.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "以下环境变量未在 .env 中配置：\n"
            + "\n".join(f"- {key}" for key in missing)
            + f"\n\n当前读取的 .env 路径：{ENV_PATH}"
        )


# ============================================================
# 创建 DeepSeek Client
# ============================================================

def create_client() -> Any:
    """
    创建 DeepSeek OpenAI-compatible Client。
    """

    check_env()

    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError(
            "OpenAI SDK is required. Install it with: python -m pip install -U openai"
        ) from error

    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=120.0,
        max_retries=1,
    )


# ============================================================
# 读取 content_list
# ============================================================

def load_content_list(path: str) -> list[dict]:
    """
    读取 MinerU 生成的 content_list.json。
    """

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(
            "content_list JSON 顶层必须是 list"
        )

    return data


# ============================================================
# 文本预处理
# ============================================================

def clean_text(text: str) -> str:
    """
    清理多余换行和空白。
    """

    if not text:
        return ""

    text = str(text)
    text = text.replace("\r", "")

    # 连续三个以上换行压缩成两个
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def truncate(
        text: str,
        max_chars: int = MAX_ITEM_PREVIEW_CHARS,
) -> str:
    """
    截断发送给 LLM 的单个元素内容。

    注意：
    这里只影响给 LLM 看的 boundary view，
    不影响原始 content_list。
    """

    if len(text) <= max_chars:
        return text

    return (
            text[:max_chars]
            + "...[TRUNCATED]"
    )


# ============================================================
# content_list item -> LLM View
# ============================================================

def item_to_llm_view(
        idx: int,
        item: dict,
) -> str | None:
    """
    将一个原始 content_list item 转换成
    更适合 LLM 判断 API 边界的形式。

    idx 始终对应原始 content_list 的数组下标。
    """

    item_type = item.get("type", "")
    page_idx = item.get("page_idx")
    text_level = item.get("text_level")

    # --------------------------------------------------------
    # 页眉 / 页脚 / 页码一般与 API 边界无关
    # --------------------------------------------------------

    if item_type in {
        "header",
        "footer",
        "page_number",
    }:
        return None

    # --------------------------------------------------------
    # metadata
    # --------------------------------------------------------

    meta = [
        f"idx={idx}",
        f"type={item_type}",
    ]

    if text_level is not None:
        meta.append(
            f"level={text_level}"
        )

    if page_idx is not None:
        meta.append(
            f"page={page_idx}"
        )

    header = (
            "["
            + " ".join(meta)
            + "]"
    )

    # --------------------------------------------------------
    # 普通文本
    # --------------------------------------------------------

    if item_type == "text":

        text = clean_text(
            item.get("text", "")
        )

        if not text:
            return None

        return (
            f"{header}\n"
            f"{truncate(text)}"
        )

    # --------------------------------------------------------
    # 表格
    #
    # API 边界判断不需要完整表格，
    # 主要保留：
    # - caption
    # - footnote
    # - body 前 600 字
    # --------------------------------------------------------

    if item_type == "table":

        caption = (
                item.get("table_caption")
                or []
        )

        footnote = (
                item.get("table_footnote")
                or []
        )

        body = clean_text(
            item.get(
                "table_body",
                "",
            )
        )

        parts = []

        if caption:
            parts.append(
                "caption: "
                + " | ".join(
                    str(x)
                    for x in caption
                )
            )

        if footnote:
            parts.append(
                "footnote: "
                + " | ".join(
                    str(x)
                    for x in footnote
                )
            )

        if body:
            parts.append(
                "table_preview: "
                + truncate(
                    body,
                    600,
                )
            )

        if not parts:
            parts.append(
                "[table]"
            )

        return (
                header
                + "\n"
                + "\n".join(parts)
        )

    # --------------------------------------------------------
    # 代码块
    #
    # URI / curl / request 示例对识别 API 很有帮助
    # --------------------------------------------------------

    if item_type == "code":

        body = clean_text(
            item.get("code_body")
            or item.get("text")
            or ""
        )

        if not body:
            return None

        return (
            f"{header}\n"
            f"{truncate(body, 1000)}"
        )

    # --------------------------------------------------------
    # 图片
    #
    # 图片本身不给 LLM，
    # 只保留 caption。
    # --------------------------------------------------------

    if item_type == "image":

        caption = (
                item.get("image_caption")
                or []
        )

        if not caption:
            return None

        return (
                f"{header}\n"
                "image_caption: "
                + " | ".join(
            str(x)
            for x in caption
        )
        )

    # --------------------------------------------------------
    # 其他未知类型
    # --------------------------------------------------------

    text = (
            item.get("text")
            or item.get("content")
            or ""
    )

    text = clean_text(text)

    if text:
        return (
            f"{header}\n"
            f"{truncate(text)}"
        )

    return None


# ============================================================
# 构造整个 LLM Boundary View
# ============================================================

def build_llm_view(
        content_list: list[dict],
) -> str:
    """
    例如：

    [idx=100 type=text level=2 page=10]
    4.1.1 获取 AccessToken - ShowOauth2Token

    [idx=101 type=text page=10]
    功能介绍

    [idx=102 type=text page=10]
    获取 Access Token

    ...
    """

    # idx 必须与原 content_list 下标完全一致，模型返回的边界才可直接切片。
    blocks = []

    for idx, item in enumerate(
            content_list
    ):

        block = item_to_llm_view(
            idx,
            item,
        )

        if block:
            blocks.append(block)

    return "\n\n".join(blocks)


# ============================================================
# DeepSeek System Prompt
# ============================================================

SYSTEM_PROMPT = """
你是一名专业的 API 接口文档结构解析器。

你的任务不是提取接口参数，而是识别一个 API 文档中每一个
“真实 API 接口”的完整边界。

输入内容来自 PDF 解析后的 content_list。

每个内容块可能包含：

idx：原始 content_list 中的数组下标
type：text / table / code / image 等
level：可能存在的标题层级
page：PDF 页码

你的核心任务是：

识别所有真实 API 接口，并确定每个接口在原始
content_list 中的 start_idx 和 end_idx。


==================================================
一、什么算真实 API 接口
==================================================

一个真实 API 接口通常包含：

接口名称
功能介绍
URI
HTTP Method
请求 Header
请求 Query 参数
请求 Path 参数
请求 Body
响应参数
请求示例
响应示例
状态码
错误码

例如：

4.1.1 获取 AccessToken - ShowOauth2Token

功能介绍

获取 Access Token。

URI

POST /orgid/openapi/v1/oauth2/token

请求参数

...

响应参数

...

请求示例

...

响应示例

...

状态码

...

错误码

...

这一整段属于一个 API 接口。


==================================================
二、非常重要：以下内容不要识别成独立 API
==================================================

以下内容不是独立 API：

1. API 概览中的接口列表
2. 目录
3. “如何调用 API”这样的教程章节
4. 教程中用于演示调用方式的 API 请求
5. HTTP Method 介绍
6. 公共请求头
7. 公共请求参数说明
8. 全局状态码表
9. 全局错误码表
10. 附录
11. 修订记录
12. API 分类标题
13. 父级章节标题


例如：

3 如何调用 API

里面可能出现：

POST /orgid/openapi/v1/oauth2/token

这只是教程示例。

不能因为出现了 POST + URI，
就把它识别成一个真实 API。


==================================================
三、start_idx 规则
==================================================

start_idx：

必须是“真实 API 接口标题”对应的 idx。

不要把父级分类标题包含进来。


例如：

[idx=100]
4.1 基于 OAuth 的应用认证集成

[idx=101]
4.1.1 获取 AccessToken - ShowOauth2Token


正确：

start_idx = 101

错误：

start_idx = 100


==================================================
四、end_idx 规则
==================================================

end_idx：

应该是属于当前 API 接口的最后一个 content_list 元素。

通常可以包含：

请求参数
响应参数
请求示例
响应示例
状态码
错误码


但是不能包含：

下一个 API 标题
下一个 API 分类章节
附录
全局错误码
全局状态码
其他公共说明章节


==================================================
五、判断接口时综合使用语义
==================================================

不要只通过某一个规则判断。

应该综合考虑：

1. 标题层级
2. 标题编号
3. 接口名称
4. URI
5. HTTP Method
6. 请求参数
7. 响应参数
8. 请求示例
9. 响应示例
10. 上下文语义
11. 当前章节是否属于真正的 API 定义章节


==================================================
六、输出格式
==================================================

必须严格输出 JSON object。

格式：

{
  "apis": [
    {
      "api_name": "获取 AccessToken",
      "api_id": "ShowOauth2Token",
      "method": "POST",
      "path": "/orgid/openapi/v1/oauth2/token",
      "start_idx": 101,
      "end_idx": 135,
      "confidence": 0.98
    }
  ]
}


字段说明：

api_name：
接口中文名称。
如果无法精确拆分，使用接口标题原文。

api_id：
接口英文名称、operationId 或类似唯一接口标识。
无法判断时返回 null。

method：
GET / POST / PUT / DELETE / PATCH 等。
无法判断时返回 null。

path：
接口 URI path。
优先只返回 path，不返回 domain。

例如：

/orgid/openapi/v1/oauth2/token

无法判断时返回 null。

start_idx：
接口开始位置。

end_idx：
接口结束位置。

confidence：
模型对当前接口边界判断的置信度。
范围 0~1。


==================================================
七、强制要求
==================================================

1. apis 必须按照 start_idx 从小到大排序。

2. 不允许不同 API 的区间发生重叠。

3. 不允许凭空创造不存在的 idx。

4. start_idx 和 end_idx 必须来自输入中的真实 idx。

5. 必须扫描完整输入内容。

6. 宁可漏掉高度不确定的接口，
   也不要把 API 教程示例误认为真实 API。

7. 输出只能包含 JSON。

8. 不要输出 Markdown。

9. 不要输出解释文字。

10. 不要在 JSON 前后输出其他文字。
"""


# ============================================================
# 调用 DeepSeek
# ============================================================

def call_deepseek(
        client: Any,
        llm_view: str,
) -> dict:
    """请求模型识别接口边界；不在此函数里创建或移动任何接口文件。"""

    user_prompt = f"""
下面是待分析的 content_list 边界视图。

请识别其中所有真实 API 接口，
并返回每个接口的 start_idx 和 end_idx。

===== CONTENT LIST START =====

{llm_view}

===== CONTENT LIST END =====
"""

    last_error = None

    for attempt in range(
            1,
            MAX_RETRY + 1,
    ):

        try:

            print(
                f"[DeepSeek] "
                f"第 {attempt}/{MAX_RETRY} 次请求..."
            )

            print(
                f"[DeepSeek] model = "
                f"{DEEPSEEK_MODEL}"
            )

            response = (
                client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": SYSTEM_PROMPT,
                        },
                        {
                            "role": "user",
                            "content": user_prompt,
                        },
                    ],
                    stream=False,
                    reasoning_effort="high",
                    extra_body={
                        "thinking": {
                            "type": "enabled"
                        }
                    },
                )
            )

            content = (
                response
                .choices[0]
                .message
                .content
            )

            if not content:
                raise ValueError(
                    "DeepSeek 返回 content 为空"
                )

            result = json.loads(
                content
            )

            if not isinstance(
                    result,
                    dict,
            ):
                raise ValueError(
                    "DeepSeek 返回结果"
                    "不是 JSON object"
                )

            if "apis" not in result:
                raise ValueError(
                    "DeepSeek 返回 JSON "
                    "中不存在 apis 字段"
                )

            if not isinstance(
                    result["apis"],
                    list,
            ):
                raise ValueError(
                    "apis 必须为 list"
                )

            return result

        except Exception as error:

            last_error = error

            print(
                "[DeepSeek] 调用失败："
                f"{error}"
            )

            if attempt < MAX_RETRY:
                time.sleep(
                    2 * attempt
                )

    raise RuntimeError(
        "DeepSeek 连续调用失败："
        f"{last_error}"
    )


# ============================================================
# HTTP Method 标准化
# ============================================================

def normalize_method(
        method,
):
    """
    将 HTTP Method 统一转换为大写。
    """

    if method is None:
        return None

    method = (
        str(method)
        .strip()
        .upper()
    )

    return method


# ============================================================
# 校验 DeepSeek 返回边界
# ============================================================

def validate_boundaries(
        result: dict,
        content_list: list[dict],
) -> list[dict]:
    """校验并规范模型边界，拒绝越界、倒序和结构不完整的接口记录。"""

    content_count = len(
        content_list
    )

    valid_apis = []

    # --------------------------------------------------------
    # 基础检查
    # --------------------------------------------------------

    for raw_api in result.get(
            "apis",
            [],
    ):

        try:
            start_idx = int(
                raw_api["start_idx"]
            )

            end_idx = int(
                raw_api["end_idx"]
            )

        except (
                KeyError,
                TypeError,
                ValueError,
        ):

            print(
                "[WARN] 非法边界，跳过："
                f"{raw_api}"
            )

            continue

        # ----------------------------------------------------
        # start_idx 越界
        # ----------------------------------------------------

        if not (
                0
                <= start_idx
                < content_count
        ):

            print(
                "[WARN] start_idx 越界："
                f"{start_idx}"
            )

            continue

        # ----------------------------------------------------
        # end_idx 越界
        # ----------------------------------------------------

        if not (
                0
                <= end_idx
                < content_count
        ):

            print(
                "[WARN] end_idx 越界："
                f"{end_idx}"
            )

            continue

        # ----------------------------------------------------
        # start > end
        # ----------------------------------------------------

        if start_idx > end_idx:

            print(
                "[WARN] start_idx > end_idx："
                f"{start_idx} > {end_idx}"
            )

            continue

        api = {
            "api_name": raw_api.get(
                "api_name"
            ),
            "api_id": raw_api.get(
                "api_id"
            ),
            "method": normalize_method(
                raw_api.get(
                    "method"
                )
            ),
            "path": raw_api.get(
                "path"
            ),
            "start_idx": start_idx,
            "end_idx": end_idx,
            "confidence": raw_api.get(
                "confidence"
            ),
        }

        valid_apis.append(api)

    # --------------------------------------------------------
    # 按 start_idx 排序
    # --------------------------------------------------------

    valid_apis.sort(
        key=lambda x: x["start_idx"]
    )

    # --------------------------------------------------------
    # start_idx 去重
    # --------------------------------------------------------

    deduped = []
    seen_start_idx = set()

    for api in valid_apis:

        start_idx = api[
            "start_idx"
        ]

        if start_idx in seen_start_idx:

            print(
                "[WARN] 出现重复 start_idx："
                f"{start_idx}，跳过"
            )

            continue

        seen_start_idx.add(
            start_idx
        )

        deduped.append(
            api
        )

    # --------------------------------------------------------
    # 防止 API 区间重叠
    #
    # 如果：
    #
    # API A: 100 -> 150
    # API B: 140 -> 200
    #
    # 自动修正：
    #
    # API A: 100 -> 139
    # API B: 140 -> 200
    # --------------------------------------------------------

    for i in range(
            len(deduped) - 1
    ):

        current_api = deduped[i]
        next_api = deduped[i + 1]

        if (
                current_api["end_idx"]
                >=
                next_api["start_idx"]
        ):

            old_end_idx = (
                current_api[
                    "end_idx"
                ]
            )

            new_end_idx = (
                    next_api[
                        "start_idx"
                    ]
                    - 1
            )

            current_api[
                "end_idx"
            ] = new_end_idx

            print(
                "[WARN] API 区间发生重叠，"
                "自动修正："
                f"{old_end_idx}"
                " -> "
                f"{new_end_idx}"
            )

    return deduped


# ============================================================
# 获取单个 item 可搜索文本
# ============================================================

def item_searchable_text(
        item: dict,
) -> str:
    """汇总 content_list 条目的文本、表格、代码等可搜索内容。"""

    fields = [
        item.get(
            "text",
            "",
        ),
        item.get(
            "table_body",
            "",
        ),
        item.get(
            "code_body",
            "",
        ),
        item.get(
            "content",
            "",
        ),
    ]

    caption = item.get(
        "table_caption"
    )

    if isinstance(
            caption,
            list,
    ):
        fields.extend(
            str(x)
            for x in caption
        )

    return "\n".join(
        str(x)
        for x in fields
        if x
    )


# ============================================================
# 简单 sanity check
# ============================================================

def verify_api_content(
        api: dict,
        content_list: list[dict],
):
    """
    如果 DeepSeek 返回 path，
    检查该 path 是否真的存在于
    切割后的 content_list 中。
    """

    path = api.get(
        "path"
    )

    if not path:
        return

    start_idx = api[
        "start_idx"
    ]

    end_idx = api[
        "end_idx"
    ]

    joined_text = "\n".join(
        item_searchable_text(item)
        for item in content_list[
                    start_idx:
                    end_idx + 1
                    ]
    )

    if path not in joined_text:

        print(
            "[WARN] 接口 "
            f"{api.get('api_name')} "
            "的 path 未在切片中找到："
            f"{path}"
        )


# ============================================================
# 安全文件名
# ============================================================

def safe_filename(
        text: str,
) -> str:
    """把接口名称清洗为跨 Windows/Linux 可用的安全文件名。"""

    if not text:
        return "unknown_api"

    text = str(text)

    # Windows/Linux 非法文件名字符
    text = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        text,
    )

    text = re.sub(
        r"\s+",
        "_",
        text,
    )

    return text[:100]


# ============================================================
# 保存 API 切割结果
# ============================================================

def save_split_results(
        apis: list[dict],
        content_list: list[dict],
        output_dir: Path,
        markdown_output_dir: Path | None = None,
        strict_markdown: bool = False,
):
    """按已验证边界切片，并生成 JSON、Markdown 与 split manifest。"""

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    interface_dir = (
            output_dir
            / "interfaces"
    )

    interface_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # 保存所有 API 边界
    # --------------------------------------------------------

    boundary_path = (
            output_dir
            / "api_boundaries.json"
    )

    with boundary_path.open(
            "w",
            encoding="utf-8",
    ) as f:

        json.dump(
            {
                "api_count": len(
                    apis
                ),
                "apis": apis,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"[保存] API 边界："
        f"{boundary_path}"
    )

    # --------------------------------------------------------
    # 每个 API 单独保存
    # --------------------------------------------------------

    markdown_records = []

    for index, api in enumerate(
            apis,
            start=1,
    ):

        start_idx = api[
            "start_idx"
        ]

        end_idx = api[
            "end_idx"
        ]

        # ====================================================
        # 真正从原始 content_list 中切割
        # ====================================================

        api_content_list = (
            content_list[
            start_idx:
            end_idx + 1
            ]
        )

        # ----------------------------------------------------
        # 文件名优先使用 api_id
        # ----------------------------------------------------

        filename_base = (
                api.get(
                    "api_id"
                )
                or api.get(
            "api_name"
        )
                or f"api_{index}"
        )

        filename = (
            f"{index:03d}_"
            f"{safe_filename(filename_base)}"
            ".json"
        )

        output_path = (
                interface_dir
                / filename
        )

        output_data = {
            "metadata": api,

            # 保留完整原始结构
            "content_list": (
                api_content_list
            ),
        }

        with output_path.open(
                "w",
                encoding="utf-8",
        ) as f:

            json.dump(
                output_data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        print(
            f"[保存] "
            f"{api.get('api_name')} "
            f"[{start_idx}, {end_idx}] "
            f"-> {output_path}"
        )

        # ----------------------------------------------------
        # 可选：同时输出单接口 Markdown
        #
        # 默认写入 content_list 同级目录，使原始 img_path
        # （例如 images/xxx.jpg）继续相对指向原 images/ 目录。
        # 不重写、不复制图片路径。
        # ----------------------------------------------------

        if markdown_output_dir is not None:

            markdown = content_list_to_markdown(
                api_content_list,
                strict=strict_markdown,
            )

            markdown_path = (
                markdown_output_dir
                / Path(filename).with_suffix(".md")
            )

            markdown_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            markdown_path.write_text(
                markdown,
                encoding="utf-8",
            )

            markdown_records.append(
                {
                    "api_id": api.get("api_id"),
                    "api_name": api.get("api_name"),
                    "markdown": str(markdown_path),
                    "slice_json": str(output_path),
                }
            )

            print(
                f"[保存] 接口 Markdown："
                f"{markdown_path}"
            )

    manifest_path = (
        output_dir
        / "split_manifest.json"
    )

    with manifest_path.open(
            "w",
            encoding="utf-8",
    ) as f:

        json.dump(
            {
                "api_count": len(apis),
                "boundaries": str(boundary_path),
                "interfaces_dir": str(interface_dir),
                "markdown_output_dir": str(markdown_output_dir) if markdown_output_dir else None,
                "markdown_files": markdown_records,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"[保存] 切分 Manifest："
        f"{manifest_path}"
    )

    return manifest_path


# ============================================================
# main
# ============================================================

def main():
    """执行模型语义切分、区间校验以及接口 JSON/Markdown 落盘。"""

    parser = argparse.ArgumentParser(
        description=(
            "使用 DeepSeek "
            "按 API 接口粒度切割 "
            "MinerU content_list"
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        help=(
            "MinerU content_list.json "
            "文件路径"
        ),
    )

    parser.add_argument(
        "--output",
        default="./api_split_output",
        help="结果输出目录",
    )

    parser.add_argument(
        "--markdown-output",
        default=None,
        help=(
            "单接口 Markdown 输出目录。默认与 input content_list.json 同级，"
            "以保持 images/... 相对路径。"
        ),
    )

    parser.add_argument(
        "--no-markdown",
        action="store_true",
        help="仅保存接口切片 JSON，不生成单接口 Markdown。",
    )

    parser.add_argument(
        "--strict-markdown",
        action="store_true",
        help="生成接口 Markdown 时遇到未知 MinerU block type 直接失败。",
    )

    args = parser.parse_args()

    # ========================================================
    # 0. 检查环境变量
    # ========================================================

    check_env()

    print(
        "[INFO] .env 路径："
        f"{ENV_PATH}"
    )

    print(
        "[INFO] DeepSeek Base URL："
        f"{DEEPSEEK_BASE_URL}"
    )

    print(
        "[INFO] DeepSeek Model："
        f"{DEEPSEEK_MODEL}"
    )

    # ========================================================
    # 1. 读取 content_list
    # ========================================================

    input_path = Path(
        args.input
    ).expanduser().resolve()

    content_list = (
        load_content_list(
            input_path
        )
    )

    print(
        "[INFO] content_list "
        f"元素数量：{len(content_list)}"
    )

    # ========================================================
    # 2. 构造 LLM Boundary View
    # ========================================================

    llm_view = (
        build_llm_view(
            content_list
        )
    )

    print(
        "[INFO] LLM 输入字符数："
        f"{len(llm_view):,}"
    )

    # ========================================================
    # 3. 创建输出目录
    # ========================================================

    output_dir = Path(
        args.output
    ).expanduser().resolve()

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # 4. 保存 LLM View
    #
    # 调试时非常重要，
    # 可以直接查看模型到底看到了什么。
    # ========================================================

    llm_view_path = (
            output_dir
            / "llm_boundary_view.txt"
    )

    llm_view_path.write_text(
        llm_view,
        encoding="utf-8",
    )

    print(
        "[INFO] LLM Boundary View："
        f"{llm_view_path}"
    )

    # ========================================================
    # 5. 创建 DeepSeek Client
    # ========================================================

    client = create_client()

    # ========================================================
    # 6. 调用 DeepSeek
    # ========================================================

    raw_result = call_deepseek(
        client=client,
        llm_view=llm_view,
    )

    # ========================================================
    # 7. 保存 DeepSeek 原始返回
    # ========================================================

    raw_result_path = (
            output_dir
            / "deepseek_raw_result.json"
    )

    raw_result_path.write_text(
        json.dumps(
            raw_result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "[INFO] DeepSeek 原始结果："
        f"{raw_result_path}"
    )

    # ========================================================
    # 8. 校验 API 边界
    # ========================================================

    apis = validate_boundaries(
        result=raw_result,
        content_list=content_list,
    )

    print()
    print(
        "[INFO] 共识别到 "
        f"{len(apis)} 个 API"
    )
    print()

    # ========================================================
    # 9. 打印接口
    # ========================================================

    for api in apis:

        verify_api_content(
            api,
            content_list,
        )

        print(
            f"{api['start_idx']:>5}"
            " -> "
            f"{api['end_idx']:<5} "
            f"{str(api['method']):<8} "
            f"{api['api_name']} "
            f"{api['path'] or ''}"
        )

    # ========================================================
    # 10. 真正切割原始 content_list
    # ========================================================

    markdown_output_dir = None

    if not args.no_markdown:

        markdown_output_dir = (
            Path(args.markdown_output)
            .expanduser()
            .resolve()
            if args.markdown_output
            else input_path.parent
        )

        if markdown_output_dir != input_path.parent:

            images_dir = (
                markdown_output_dir
                / "images"
            )

            if not images_dir.is_dir():

                raise ValueError(
                    "--markdown-output 不与 content_list 同级时，"
                    "该目录必须已有 images/，否则 images/... 相对路径会失效："
                    f"{images_dir}"
                )

    save_split_results(
        apis=apis,
        content_list=content_list,
        output_dir=output_dir,
        markdown_output_dir=markdown_output_dir,
        strict_markdown=args.strict_markdown,
    )

    print()
    print(
        "[DONE] API 接口切割完成。"
    )


if __name__ == "__main__":
    main()
