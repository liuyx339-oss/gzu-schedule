#!/usr/bin/env python3
"""
GZU 排班系统 — 本地服务器
=========================================
- 提供排班仪表盘网页访问
- 处理排班修改请求（保存到本地 JSON + 可选飞书同步）

启动: python server.py [--port 8765]
"""

from __future__ import annotations

import json
import os
import sys
import time
import argparse
import traceback
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler

# ── 尝试加载 .env ──────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "schedule_data.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "modification_log.json")

# ── 飞书配置（可选）─────────────────────────────────────
FEISHU_ENABLED = bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_BASE_ID"))
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_BASE = os.environ.get("FEISHU_BASE_ID", "")
FEISHU_TABLE_LOG = os.environ.get("FEISHU_TABLE_LOG_ID", "")


def _get_feishu_token() -> str | None:
    if not FEISHU_ENABLED:
        return None
    try:
        import requests
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        )
        data = resp.json()
        return data.get("tenant_access_token")
    except Exception as e:
        print(f"[FEISHU] Token error: {e}")
        return None


def _sync_to_feishu(record: dict) -> bool:
    if not FEISHU_ENABLED:
        return False
    token = _get_feishu_token()
    if not token:
        return False
    try:
        import requests
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if FEISHU_TABLE_LOG:
            log_payload = {
                "fields": {
                    "日期": record.get("date", ""),
                    "角色": record.get("role", ""),
                    "班次": record.get("shift", ""),
                    "原人员": record.get("old_person", ""),
                    "新人员": record.get("new_person", ""),
                    "修改人": record.get("modifier", "未知"),
                    "修改时间": record.get("time", ""),
                    "修改原因": record.get("reason", ""),
                }
            }
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE}/tables/{FEISHU_TABLE_LOG}/records"
            resp = requests.post(url, headers=headers, json=log_payload, timeout=10)
            if resp.status_code == 200:
                print(f"[FEISHU] Synced: {record['date']} {record['role']} {record['old_person']} -> {record['new_person']}")
                return True
            print(f"[FEISHU] Sync failed: {resp.status_code}")
        return False
    except Exception as e:
        print(f"[FEISHU] Sync error: {e}")
        return False


class ScheduleHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def log_message(self, format, *args):
        print(f"  [{self.command}] {args[0]}")

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path == "/api/schedule":
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._send_json({"success": True, "data": data})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
        elif self.path == "/api/log":
            log = []
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    log = json.load(f)
            self._send_json({"success": True, "data": log})
        elif self.path == "/api/status":
            self._send_json({
                "success": True,
                "data": {
                    "data_file_exists": os.path.exists(DATA_FILE),
                    "feishu_enabled": FEISHU_ENABLED,
                    "server_time": datetime.now().isoformat(),
                }
            })
        else:
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        # 尝试 UTF-8，失败则尝试 GBK（兼容 Windows curl 测试）
        try:
            body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            body = raw_body.decode("gbk", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"success": False, "error": "Invalid JSON"}, 400)
            return

        if self.path == "/api/save":
            self._handle_save(payload)
        else:
            self._send_json({"success": False, "error": "Unknown API"}, 404)

    def _handle_save(self, payload: dict):
        role = payload.get("role", "")
        staff_name = payload.get("staff_name", "")
        date = payload.get("date", "")
        new_shift = payload.get("new_shift", "")
        new_person = payload.get("new_person", "")
        modifier = payload.get("modifier", "unknown")
        reason = payload.get("reason", "")

        if not all([role, staff_name, date]):
            self._send_json({"success": False, "error": "Missing fields: role, staff_name, date"}, 400)
            return

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if role not in data["roles"]:
                self._send_json({"success": False, "error": f"Unknown role: {role}"}, 400)
                return

            role_data = data["roles"][role]
            old_shift = ""
            old_person = staff_name

            if new_person and new_person != staff_name:
                for s in role_data["staff"]:
                    if s["name"] == staff_name and date in s["schedule"]:
                        old_shift = s["schedule"].get(date, "")
                        s["schedule"][date] = ""
                        if date in s.get("category", {}):
                            s["category"][date] = ""
                    if s["name"] == new_person and date in s["schedule"]:
                        s["schedule"][date] = new_shift
                        if date in s.get("category", {}):
                            s["category"][date] = "人工修改"
            else:
                for s in role_data["staff"]:
                    if s["name"] == staff_name and date in s["schedule"]:
                        old_shift = s["schedule"].get(date, "")
                        s["schedule"][date] = new_shift
                        if date in s.get("category", {}):
                            s["category"][date] = "人工修改" if new_shift else ""

            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            log_entry = {
                "date": date, "role": role, "shift": new_shift,
                "old_person": old_person, "new_person": new_person or staff_name,
                "old_shift": old_shift,
                "modifier": modifier, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
            }
            log = []
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    log = json.load(f)
            log.append(log_entry)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)

            if FEISHU_ENABLED:
                _sync_to_feishu(log_entry)

            print(f"[SAVE] {date} {role} {old_person}({old_shift}) -> {new_person or staff_name}({new_shift or 'off'})")
            self._send_json({"success": True, "data": log_entry})

        except Exception as e:
            traceback.print_exc()
            self._send_json({"success": False, "error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="GZU Schedule Server")
    parser.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default 127.0.0.1)")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), ScheduleHandler)
    print("=" * 50)
    print("  GZU Radiology/Ultrasound Schedule System")
    print("=" * 50)
    print(f"  URL: http://{args.host}:{args.port}")
    print(f"  Dashboard: http://{args.host}:{args.port}/排班仪表盘.html")
    print(f"  Feishu sync: {'ENABLED' if FEISHU_ENABLED else 'DISABLED'}")
    print(f"  Press Ctrl+C to stop")
    print("=" * 50)

    import webbrowser, threading
    def _open():
        time.sleep(0.5)
        webbrowser.open(f"http://{args.host}:{args.port}/排班仪表盘.html")
    threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
