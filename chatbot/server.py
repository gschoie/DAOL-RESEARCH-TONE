"""다올 리서치 톤 챗봇 로컬 서버.

표준 라이브러리 http.server로 127.0.0.1에만 열린다.
POST /api/chat 하나와 정적 채팅 UI를 서빙한다.
"""
from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chatbot

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

MAX_HISTORY_TURNS = 20  # 요청당 전달하는 최대 대화 턴 수


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        relative = "index.html" if self.path == "/" else self.path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            return self.send_error(403)
        if not target.is_file():
            target = STATIC / "index.html"
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != "/api/chat":
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            messages = payload.get("messages", [])
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "잘못된 요청 형식입니다."}, 400)

        cleaned = [
            {"role": m["role"], "content": str(m["content"])}
            for m in messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ][-MAX_HISTORY_TURNS:]
        if not cleaned or cleaned[-1]["role"] != "user":
            return self.send_json({"error": "마지막 메시지는 user여야 합니다."}, 400)

        try:
            reply = chatbot.answer(cleaned)
        except Exception as exc:  # 사용자에게는 요약만, 상세는 서버 로그로
            print(f"[chat error] {exc!r}")
            return self.send_json({"error": "답변 생성 중 오류가 발생했습니다. API 키와 네트워크를 확인하세요."}, 500)
        return self.send_json({"reply": reply})


if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("경고: ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 또는 환경변수로 설정하세요.")
    port = int(os.getenv("PORT", "8788"))
    print(f"다올 리서치 톤 챗봇: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
