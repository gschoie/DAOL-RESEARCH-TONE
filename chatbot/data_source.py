"""다올 리서치 톤 데이터 로더 — 로컬 우선, GitHub Pages 폴백.

이 챗봇은 데이터를 생산하는 DAOL-RESEARCH-TONE 리포 안에 살고 있으므로,
리포의 data/ 디렉토리가 있으면 그것을 직접 읽는다(파일 mtime 기반 메모리 캐시).
리포 밖에서 단독 실행되면 GitHub Pages JSON(CORS 개방)을 TTL 캐시로 받아온다.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.getenv("DAOL_TONE_BASE_URL", "https://gschoie.github.io/DAOL-RESEARCH-TONE/")
LOCAL_DATA_DIR = Path(os.getenv("DAOL_TONE_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
CACHE_DIR = Path(os.getenv("DAOL_CHATBOT_CACHE_DIR", Path(__file__).resolve().parent / ".cache"))
DEFAULT_TTL_SECONDS = int(os.getenv("DAOL_CHATBOT_CACHE_TTL", "600"))

# 파싱된 JSON 메모리 캐시: name -> (mtime 또는 fetch 시각, 데이터)
_memory: dict[str, tuple[float, Any]] = {}


def _load_local(name: str) -> Any | None:
    path = LOCAL_DATA_DIR / name
    if not path.is_file():
        return None
    mtime = path.stat().st_mtime
    cached = _memory.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    _memory[name] = (mtime, data)
    return data


def _load_remote(name: str, ttl_seconds: int) -> Any:
    cached = _memory.get(name)
    if cached and (time.time() - cached[0]) < ttl_seconds:
        return cached[1]

    disk = CACHE_DIR / name
    if disk.is_file() and (time.time() - disk.stat().st_mtime) < ttl_seconds:
        data = json.loads(disk.read_text(encoding="utf-8"))
        _memory[name] = (time.time(), data)
        return data

    url = BASE_URL.rstrip("/") + "/" + name
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        disk.write_text(raw, encoding="utf-8")
        _memory[name] = (time.time(), data)
        return data
    except Exception:
        # 네트워크 실패: 만료된 디스크 캐시라도 있으면 사용
        if disk.is_file():
            data = json.loads(disk.read_text(encoding="utf-8"))
            _memory[name] = (time.time(), data)
            return data
        raise


def fetch_json(name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Any:
    local = _load_local(name)
    if local is not None:
        return local
    return _load_remote(name, ttl_seconds)


def load_tone_v2() -> dict:
    """메인 데이터 (~3MB): sectors/companies/events/steady + street."""
    return fetch_json("daol_tone_v2.json")


def load_summary() -> dict:
    """경량 요약: 최신 리포트 20건 + 이벤트 20건 + 카운트."""
    return fetch_json("tone_summary.json")


def load_ked_street() -> dict | list:
    """전시장 타사 리포트 120일치 — 다올 미커버 종목 질의용."""
    return fetch_json("ked_street.json")


def load_pdf_text_cache() -> dict:
    """리포트 PDF 원문 캐시 (~14MB, 1,270여 건): source_url -> {status, text, ...}."""
    return fetch_json("daol_pdf_text_cache.json")


def load_messages() -> list:
    """텔레그램 원 메시지 목록 — PDF 원문의 메타(날짜/제목) 폴백 조인용."""
    return fetch_json("daol_messages.json")
