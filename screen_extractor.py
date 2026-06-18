#!/usr/bin/env python3
"""
screen_extractor.py — 屏幕内容一键提取小工具
================================================
悬浮窗 → 点击"开始"录屏 → 翻看对话框 → 点击"结束"停止
→ 千问 VL 视频理解 → 内容自动写入飞书 Wiki

零云服务依赖（除千问 API 外），视频通过千问自带临时存储上传，
48 小时后自动清理，全程无残留。

依赖（一次性安装）:
  pip install mss pillow opencv-python requests

环境变量:
  DASHSCOPE_API_KEY   千问 API Key
  FEISHU_APP_ID       飞书应用 ID
  FEISHU_APP_SECRET   飞书应用密钥

用法:
  python screen_extractor.py
"""

import os
import sys
import time
import tempfile
import threading
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

# =============================================
# CHECK DEPENDENCIES
# =============================================

MISSING = []
try:
    import mss
except ImportError:
    MISSING.append("mss")
try:
    from PIL import Image
except ImportError:
    MISSING.append("Pillow")
try:
    import cv2
    import numpy as np
except ImportError:
    MISSING.append("opencv-python")
try:
    import requests
except ImportError:
    MISSING.append("requests")

if MISSING:
    print("缺少依赖，请先安装：")
    print(f"  pip install {' '.join(MISSING)}")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk, messagebox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("screen_extractor")

# =============================================
# CONFIG
# =============================================

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# 录屏参数
CAPTURE_INTERVAL = 0.5      # 截图间隔（秒）
MAX_WIDTH = 1920
JPEG_QUALITY = 80

# 默认 Wiki 节点
DEFAULT_NODE = "DTaUwC2Q4i8AJYkWoExcGjQVnZx"
FEISHU_API = "https://open.feishu.cn/open-apis"

# 千问 API
DASHSCOPE_UPLOAD    = "https://dashscope.aliyuncs.com/api/v1/uploads"
DASHSCOPE_GENERATE  = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

# =============================================
# 全局状态
# =============================================

_session = None
_capturing = False
_frames = []
_frame_count = 0
_status_text = None
_start_btn = None
_stop_btn = None
root = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
    return _session


def _check_env():
    msgs = []
    if not DASHSCOPE_API_KEY: msgs.append("DASHSCOPE_API_KEY")
    if not FEISHU_APP_ID:     msgs.append("FEISHU_APP_ID")
    if not FEISHU_APP_SECRET: msgs.append("FEISHU_APP_SECRET")
    if msgs:
        messagebox.showerror("环境变量缺失",
            f"请设置:\n  " + "\n  ".join(msgs))
        sys.exit(1)


# =============================================
# 截屏（内存中，不写磁盘）
# =============================================


def _capture_loop():
    global _capturing, _frames, _frame_count
    log.info("开始录屏...")
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        while _capturing:
            try:
                img = sct.grab(monitor)
                pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                w, h = pil_img.size
                if w > MAX_WIDTH:
                    ratio = MAX_WIDTH / w
                    pil_img = pil_img.resize((MAX_WIDTH, int(h * ratio)), Image.LANCZOS)
                buf = BytesIO()
                pil_img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                buf.seek(0)
                _frames.append(Image.open(buf).convert("RGB"))
                _frame_count += 1
                root.after(0, lambda: _status_text.set(
                    f"● 录屏中  |  {_frame_count} 帧  |  ~{int(_frame_count * CAPTURE_INTERVAL)}s"))
            except Exception as e:
                log.error(f"截屏失败: {e}")
            time.sleep(CAPTURE_INTERVAL)


# =============================================
# 千问：上传视频到临时存储（无需自己OSS）
# =============================================


def _upload_to_dashscope(video_path):
    """将视频上传到千问内置临时存储，返回 oss:// URL

    千问提供免费临时存储（48h 有效，自动清理），不需要自己的 OSS。
    """
    filename = Path(video_path).name
    size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    log.info(f"上传到千问临时存储: {filename} ({size_mb:.1f} MB)")

    # Step 1: 获取上传凭证
    resp = _get_session().get(
        DASHSCOPE_UPLOAD,
        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"},
        params={"action": "getPolicy", "model": "qwen-vl-max"},
        timeout=(10, 30),
    )
    data = resp.json()
    if data.get("code") and data["code"] != 0:
        raise RuntimeError(f"千问上传凭证失败: {data.get('message', data.get('msg', ''))}")

    policy = data.get("data", data)
    upload_host = policy["upload_host"]
    upload_dir = policy["upload_dir"]
    oss_key = f"{upload_dir}/{filename}"

    log.info(f"获取凭证成功，上传中...")

    # Step 2: 上传文件到千问的临时 OSS
    with open(video_path, "rb") as f:
        resp2 = _get_session().post(
            upload_host,
            files={
                "OSSAccessKeyId":       (None, policy["oss_access_key_id"]),
                "Signature":            (None, policy["signature"]),
                "policy":               (None, policy["policy"]),
                "x-oss-object-acl":     (None, policy.get("x_oss_object_acl", "private")),
                "x-oss-forbid-overwrite": (None, policy.get("x_oss_forbid_overwrite", "true")),
                "key":                  (None, oss_key),
                "success_action_status": (None, "200"),
                "file":                 (filename, f),
            },
            timeout=(30, 120),
        )

    if resp2.status_code != 200:
        raise RuntimeError(f"千问上传失败: HTTP {resp2.status_code}")

    oss_url = f"oss://{oss_key}"
    log.info(f"上传成功: {oss_url}")
    return oss_url


# =============================================
# 千问 VL 视频分析
# =============================================


def _analyze_video(oss_url):
    """千问 VL 视频理解 — 解析滚动对话框"""
    prompt = (
        "请仔细观察这个录屏视频中的对话框内容，完成以下任务：\n\n"
        "1. 识别所有可见的对话框文字内容\n"
        "2. 按时间顺序整理对话\n"
        "3. 区分不同说话人（如能区分客服、用户等角色）\n"
        "4. 特别标注关键信息：日期、数字、金额、承诺、要求、截止时间\n\n"
        "输出格式：\n"
        "## 对话摘要\n"
        "一段话概括对话主题\n\n"
        "## 详细对话\n"
        "按时间顺序逐条列出"
    )

    payload = {
        "model": "qwen-vl-max",
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"video": oss_url},
                    {"text": prompt},
                ],
            }]
        },
        "parameters": {"result_format": "message"},
    }

    log.info("千问 VL 分析视频中...")
    resp = _get_session().post(
        DASHSCOPE_GENERATE,
        json=payload,
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
            "X-DashScope-OssResourceResolve": "enable",  # 关键：让千问解析 oss:// URL
        },
        timeout=(30, 600),
    )
    data = resp.json()

    if data.get("code") and data["code"] != 0:
        raise RuntimeError(
            f"千问错误: code={data.get('code')} "
            f"msg={data.get('message', data.get('msg', ''))}"
        )

    # 提取文本
    try:
        choices = data.get("output", {}).get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                return "\n".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            return content
    except Exception:
        pass

    return str(data)


# =============================================
# 截帧 → 合成 MP4
# =============================================


def _frames_to_mp4(frames, output_path):
    """内存中的帧 → 临时 MP4 文件"""
    first = cv2.cvtColor(np.array(frames[0]), cv2.COLOR_RGB2BGR)
    h, w = first.shape[:2]
    fps = int(1 / CAPTURE_INTERVAL) if CAPTURE_INTERVAL > 0 else 2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (w, h))
    for pil_img in frames:
        writer.write(cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR))
    writer.release()
    return output_path


# =============================================
# 飞书 Wiki 写入
# =============================================


def _get_feishu_token():
    url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
    resp = _get_session().post(
        url,
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(10, 60),
    )
    d = resp.json()
    if d.get("code") != 0:
        raise RuntimeError(f"飞书认证失败: {d.get('msg')}")
    return d["tenant_access_token"]


def _get_wiki_doc_id(token, node_token):
    resp = _get_session().get(
        f"{FEISHU_API}/wiki/v2/spaces/get_node",
        headers={"Authorization": f"Bearer {token}"},
        params={"token": node_token},
        timeout=(10, 30),
    )
    d = resp.json()
    if d.get("code") != 0:
        raise RuntimeError(f"Wiki 节点失败: {d.get('msg')}")
    return d["data"]["node"]["obj_token"]


def _write_wiki(token, document_id, raw_text):
    BT = {
        "text": 2, "heading1": 3, "heading2": 4, "heading3": 5,
        "bullet": 12, "ordered": 13, "quote": 15, "code": 14, "divider": 22,
    }
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    children = [
        {"block_type": 22},
        {"block_type": 4, "text": {"elements": [{"text_run": {"content": f"📹 屏幕提取 — {ts}"}}]}},
        {"block_type": 22},
    ]

    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            children.append({"block_type": 4, "text": {"elements": [{"text_run": {"content": line[3:]}}]}})
        elif line.startswith("### "):
            children.append({"block_type": 5, "text": {"elements": [{"text_run": {"content": line[4:]}}]}})
        elif line.startswith("- ") or line.startswith("* "):
            children.append({"block_type": 12, "text": {"elements": [{"text_run": {"content": line[2:]}}]}})
        else:
            children.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": line}}]}})

    children.append({"block_type": 22})
    children.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": f"—— 千问 VL 自动提取 | {ts}"}}]}})

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
    d = resp.json()
    if d.get("code") != 0:
        raise RuntimeError(f"写入失败: {d.get('msg')}")


# =============================================
# 按钮事件
# =============================================


def on_start():
    global _capturing, _frames, _frame_count
    if _capturing:
        return
    _frames = []
    _frame_count = 0
    _capturing = True
    _start_btn.config(state="disabled")
    _stop_btn.config(state="normal")
    _status_text.set("● 录屏中...")
    threading.Thread(target=_capture_loop, daemon=True).start()


def on_stop():
    global _capturing
    if not _capturing:
        return
    _capturing = False
    _stop_btn.config(state="disabled")
    _status_text.set("⏳ 处理中...")
    threading.Thread(target=_process, daemon=True).start()


def _process():
    global _frames
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_path = None

    try:
        t_start = time.time()

        # Step 1: 合成 MP4
        root.after(0, lambda: _status_text.set("⏳ 合成视频..."))
        tmp_path = os.path.join(tempfile.gettempdir(), f"qwen_screen_{task_id}.mp4")
        _frames_to_mp4(_frames, tmp_path)

        total_frames = len(_frames)
        size_kb = os.path.getsize(tmp_path) / 1024
        _frames = []  # 释放内存
        log.info(f"合成完成: {size_kb:.0f} KB, {total_frames} 帧")

        # Step 2: 上传到千问临时存储
        root.after(0, lambda: _status_text.set("⏳ 上传视频到千问..."))
        oss_url = _upload_to_dashscope(tmp_path)

        # 立即清理临时 MP4
        try: os.remove(tmp_path)
        except: pass
        tmp_path = None
        log.info("本地临时文件已清理")

        # Step 3: 千问 VL 分析
        root.after(0, lambda: _status_text.set("⏳ 千问 AI 分析视频..."))
        raw_text = _analyze_video(oss_url)
        # oss_url 48h 后千问自动清理，无需手动删除

        # Step 4: 写入飞书
        root.after(0, lambda: _status_text.set("⏳ 写入飞书知识库..."))
        token = _get_feishu_token()
        doc_id = _get_wiki_doc_id(token, DEFAULT_NODE)
        _write_wiki(token, doc_id, raw_text)

        elapsed = time.time() - t_start

        root.after(0, lambda: _start_btn.config(state="normal"))
        root.after(0, lambda: _status_text.set(
            f"✅ 完成! {total_frames}帧 | {elapsed:.0f}s | 已写入 Wiki"
        ))

        preview = raw_text[:500] + ("..." if len(raw_text) > 500 else "")
        root.after(0, lambda: messagebox.showinfo(
            "提取完成",
            f"已写入: https://h03iw32mvho.feishu.cn/wiki/{DEFAULT_NODE}\n\n"
            f"预览:\n{preview}"
        ))

    except Exception as e:
        log.exception("处理失败")
        if tmp_path:
            try: os.remove(tmp_path)
            except: pass
        root.after(0, lambda: _status_text.set(f"❌ 失败: {str(e)[:60]}"))
        root.after(0, lambda: _start_btn.config(state="normal"))
        root.after(0, lambda: messagebox.showerror("错误", str(e)))


# =============================================
# UI
# =============================================


def build_ui():
    global root, _status_text, _start_btn, _stop_btn

    root = tk.Tk()
    root.title("屏幕提取")
    root.geometry("390x230")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{sw-410}+{sh-310}")

    style = ttk.Style()
    style.theme_use("clam")

    title_frame = ttk.Frame(root)
    title_frame.pack(fill="x", padx=14, pady=(14, 0))
    ttk.Label(title_frame, text="📹 千问 VL 屏幕提取",
              font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
    ttk.Label(title_frame, text="Qwen VL", foreground="gray").pack(side="right")

    ttk.Label(root,
        text="点击"开始" → 翻看对话框 → 点击"结束"",
        foreground="#666", font=("Microsoft YaHei UI", 9),
    ).pack(pady=(6, 0))

    _status_text = tk.StringVar(value="就绪")
    ttk.Label(root, textvariable=_status_text, foreground="#333",
              font=("Microsoft YaHei UI", 10), wraplength=360).pack(pady=(10, 14))

    btn_frame = ttk.Frame(root)
    btn_frame.pack()

    _start_btn = tk.Button(btn_frame, text="● 开始",
        font=("Microsoft YaHei UI", 13, "bold"),
        bg="#4CAF50", fg="white", activebackground="#388E3C", activeforeground="white",
        relief="flat", bd=0, padx=30, pady=10, cursor="hand2", command=on_start)
    _start_btn.pack(side="left", padx=10)

    _stop_btn = tk.Button(btn_frame, text="■ 结束",
        font=("Microsoft YaHei UI", 13, "bold"),
        bg="#F44336", fg="white", activebackground="#C62828", activeforeground="white",
        relief="flat", bd=0, padx=30, pady=10, cursor="hand2",
        state="disabled", command=on_stop)
    _stop_btn.pack(side="left", padx=10)

    ttk.Label(root, text="千问 VL 视频理解 | 免 OSS | 不保留任何文件",
              foreground="#aaa", font=("Microsoft YaHei UI", 8)).pack(pady=(16, 0))

    root.mainloop()


if __name__ == "__main__":
    _check_env()
    build_ui()
