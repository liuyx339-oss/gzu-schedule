#!/usr/bin/env python3
"""
video_to_wiki.py — 视频内容提取 → 飞书知识库写入
================================================================
使用千问 VL 大模型分析视频中滚动对话框的文字内容，
提取后自动写入飞书知识库指定节点。

前置:
  1. set DASHSCOPE_API_KEY=你的千问Key
  2. set FEISHU_APP_ID=xxx
  3. set FEISHU_APP_SECRET=xxx
  4. 视频需上传到 OSS（自动）或已有公开 URL

用法:
  # 本地视频 + OSS 自动上传
  set OSS_ACCESS_KEY_ID=xxx
  set OSS_ACCESS_KEY_SECRET=xxx
  set OSS_BUCKET=my-bucket
  set OSS_ENDPOINT=oss-cn-guangzhou.aliyuncs.com
  python video_to_wiki.py --video "D:\录屏.mp4"

  # 已有视频 URL
  python video_to_wiki.py --video-url "https://oss.example.com/video.mp4"

  # 试运行（不写入飞书）
  python video_to_wiki.py --video "xxx.mp4" --dry-run
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("video_to_wiki")

# =============================================
# CONFIG — 全部从环境变量读取
# =============================================

# 千问
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

# 飞书
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_API = "https://open.feishu.cn/open-apis"

# OSS（可选，仅 --video 本地文件时需要）
OSS_KEY = os.environ.get("OSS_ACCESS_KEY_ID", "")
OSS_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "oss-cn-guangzhou.aliyuncs.com")

# DashScope
DASHSCOPE_API = "https://dashscope.aliyuncs.com"

# 默认 Wiki 节点（从 https://h03iw32mvho.feishu.cn/wiki/DTaUwC2Q4i8AJYkWoExcGjQVnZx 解析）
DEFAULT_NODE = "DTaUwC2Q4i8AJYkWoExcGjQVnZx"

_session = None

# =============================================
# 工具函数
# =============================================


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
    return _session


def _check_required():
    """检查必需的环境变量"""
    errors = []
    if not DASHSCOPE_API_KEY:
        errors.append("DASHSCOPE_API_KEY（千问 API Key）")
    if not FEISHU_APP_ID:
        errors.append("FEISHU_APP_ID（飞书应用 ID）")
    if not FEISHU_APP_SECRET:
        errors.append("FEISHU_APP_SECRET（飞书应用密钥）")
    if errors:
        raise RuntimeError(
            f"缺少必需的环境变量:\n  " + "\n  ".join(errors) +
            "\n\n设置方法:\n  set 变量名=值"
        )


# =============================================
# 飞书 API
# =============================================


def get_feishu_token():
    url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
    resp = _get_session().post(
        url,
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(10, 60),
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书认证失败: {data.get('msg')}")
    return data["tenant_access_token"]


def get_wiki_node(token, node_token):
    """获取 Wiki 节点信息 → document_id, space_id"""
    url = f"{FEISHU_API}/wiki/v2/spaces/get_node"
    resp = _get_session().get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"token": node_token},
        timeout=(10, 30),
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取节点失败: {data.get('msg')}")
    node = data.get("data", {}).get("node", {})
    return {
        "title": node.get("title", ""),
        "obj_type": node.get("obj_type", ""),
        "obj_token": node.get("obj_token", ""),
        "space_id": node.get("space_id", ""),
    }


def write_docx_blocks(token, document_id, blocks):
    """向 Docx 文档末尾追加内容块

    blocks 示例:
      {"type": "heading2", "content": "标题"}
      {"type": "text", "content": "正文"}
      {"type": "bullet", "content": "列表项"}
      {"type": "divider"}
    """
    BT = {
        "text": 2, "heading1": 3, "heading2": 4, "heading3": 5,
        "heading4": 6, "heading5": 7, "heading6": 8, "heading7": 9,
        "heading8": 10, "heading9": 11,
        "bullet": 12, "ordered": 13, "code": 14, "quote": 15,
        "divider": 22,
    }

    children = []
    for b in blocks:
        bt_id = BT.get(b.get("type", "text"), 2)

        if b.get("type") == "divider":
            children.append({"block_type": bt_id})
        elif b.get("type") == "code":
            children.append({
                "block_type": bt_id,
                "code": {"elements": [{"text_run": {"content": b.get("content", "")}}]},
            })
        else:
            content = b.get("content", "")
            children.append({
                "block_type": bt_id,
                "text": {"elements": [{"text_run": {"content": content}}]},
            })

    url = f"{FEISHU_API}/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    resp = _get_session().post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"children": children},
        timeout=(10, 60),
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"写入文档失败: {data.get('msg')}")
    return data


# =============================================
# OSS 上传
# =============================================


def upload_to_oss(local_path):
    """上传本地文件到 OSS，返回公开 URL"""
    try:
        import oss2
    except ImportError:
        raise RuntimeError("需要 pip install oss2")

    if not all([OSS_KEY, OSS_SECRET, OSS_BUCKET]):
        raise RuntimeError(
            "OSS 未配置。需设置: OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET"
        )

    auth = oss2.Auth(OSS_KEY, OSS_SECRET)
    bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = f"video_to_wiki/{ts}_{Path(local_path).name}"

    log.info(f"上传到 OSS: oss://{OSS_BUCKET}/{key}")
    result = bucket.put_object_from_file(key, local_path)
    if result.status != 200:
        raise RuntimeError(f"OSS 上传失败: HTTP {result.status}")

    url = f"https://{OSS_BUCKET}.{OSS_ENDPOINT}/{key}"
    log.info(f"上传成功: {url}")
    return url


def ensure_video_url(path_or_url):
    """智能获取视频 URL"""
    p = Path(path_or_url)

    # 已经是 URL
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        log.info(f"使用已有 URL: {path_or_url[:80]}...")
        return path_or_url

    # 本地文件
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")

    size_mb = p.stat().st_size / (1024 * 1024)
    log.info(f"本地视频: {p} ({size_mb:.1f} MB)")

    # 尝试 OSS 上传
    if all([OSS_KEY, OSS_SECRET, OSS_BUCKET]):
        return upload_to_oss(str(p))

    raise RuntimeError(
        f"\n{'='*55}\n"
        f"视频 ({size_mb:.1f}MB) 需上传到公开 URL 供千问 API 下载\n\n"
        f"方案 1 [推荐] — 配置 OSS 自动上传:\n"
        f"  set OSS_ACCESS_KEY_ID=xxx\n"
        f"  set OSS_ACCESS_KEY_SECRET=xxx\n"
        f"  set OSS_BUCKET=xxx\n"
        f"  set OSS_ENDPOINT=oss-cn-guangzhou.aliyuncs.com\n\n"
        f"方案 2 — 手动上传后用 --video-url:\n"
        f"  python video_to_wiki.py --video-url \"https://...\"\n"
        f"{'='*55}"
    )


# =============================================
# 千问 VL 视频分析
# =============================================


def analyze_video(video_url, prompt=None):
    """千问 VL 分析视频，返回提取的文本"""

    DEFAULT_PROMPT = (
        "请仔细观察这个屏幕录制视频中的对话框内容（包括聊天记录、"
        "滚动消息、通知等），完成以下任务：\n\n"
        "1. 识别所有可见的对话框文字\n"
        "2. 按时间顺序整理对话内容\n"
        "3. 区分不同说话人（如果可以识别）\n"
        "4. 标注关键信息（日期、数字、要求、承诺）\n\n"
        "输出格式：先给一段摘要，再逐条列出对话。"
    )

    payload = {
        "model": "qwen-vl-max",
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"video": video_url},
                    {"text": prompt or DEFAULT_PROMPT},
                ],
            }]
        },
        "parameters": {"result_format": "message"},
    }

    log.info("调用千问 VL 分析视频...")
    resp = _get_session().post(
        f"{DASHSCOPE_API}/api/v1/services/aigc/multimodal-generation/generation",
        json=payload,
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=(30, 600),
    )
    data = resp.json()

    if data.get("code") and data["code"] != 0:
        raise RuntimeError(
            f"千问 API 错误: code={data.get('code')} msg={data.get('message', data.get('msg', ''))}"
        )

    # 提取文本
    try:
        choices = data.get("output", {}).get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                return "\n".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                )
            return content
    except Exception:
        pass

    return json.dumps(data, ensure_ascii=False, indent=2)


# =============================================
# 结果格式化
# =============================================


def format_blocks(video_name, raw_text):
    """将提取文本转为 Wiki 内容块"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blocks = [
        {"type": "divider"},
        {"type": "heading2", "content": f"📹 视频分析 — {ts}"},
        {"type": "text", "content": f"来源: {video_name}"},
        {"type": "divider"},
    ]

    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            blocks.append({"type": "heading2", "content": line[3:]})
        elif line.startswith("### "):
            blocks.append({"type": "heading3", "content": line[4:]})
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({"type": "bullet", "content": line[2:]})
        elif line.startswith("> "):
            blocks.append({"type": "quote", "content": line[2:]})
        else:
            blocks.append({"type": "text", "content": line})

    blocks.append({"type": "divider"})
    blocks.append({"type": "text", "content": f"—— Qwen VL 自动提取 | {ts}"})
    return blocks


# =============================================
# MAIN
# =============================================


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="视频 → 千问 VL → 飞书 Wiki")
    parser.add_argument("--video", type=str, default=None, help="本地视频路径（需 OSS 配置）")
    parser.add_argument("--video-url", type=str, default=None, help="视频公开 URL")
    parser.add_argument("--node-token", type=str, default=DEFAULT_NODE, help="Wiki 节点 token")
    parser.add_argument("--prompt", type=str, default=None, help="自定义提取提示词")
    parser.add_argument("--dry-run", action="store_true", help="仅分析，不写飞书")
    parser.add_argument("--output", type=str, default=None, help="结果保存到本地文件")
    args = parser.parse_args()

    if not args.video and not args.video_url:
        parser.error("必须指定 --video 或 --video-url")

    # Step 0: 检查环境
    _check_required()

    # Step 1: 获取视频 URL
    video_source = args.video_url or args.video
    video_url = ensure_video_url(video_source)
    video_name = Path(args.video).name if args.video else video_url.split("/")[-1][:50]

    # Step 2: 千问分析
    log.info("=" * 50)
    raw = analyze_video(video_url, args.prompt)

    print("\n" + "=" * 60)
    print("📹 提取结果")
    print("=" * 60)
    print(raw)
    print("=" * 60 + "\n")

    # 可选: 保存本地
    if args.output:
        p = Path(args.output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw, encoding="utf-8")
        log.info(f"已保存: {p}")

    if args.dry_run:
        log.info("[DRY RUN] 完成，未写入飞书")
        return

    # Step 3: 写入飞书 Wiki
    token = get_feishu_token()
    node = get_wiki_node(token, args.node_token)
    log.info(f"Wiki: {node['title']} | {node['obj_type']} | doc_id={node['obj_token']}")

    blocks = format_blocks(video_name, raw)
    write_docx_blocks(token, node["obj_token"], blocks)

    log.info(f"✅ 完成! https://h03iw32mvho.feishu.cn/wiki/{args.node_token}")


if __name__ == "__main__":
    main()
