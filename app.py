from __future__ import annotations

import os
from dotenv import load_dotenv # .env 파일 로드를 위해 추가

# .env 파일을 읽어 환경 변수를 로드합니다. (로컬 실행 시 필요)
load_dotenv() 

import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional, TYPE_CHECKING # TYPE_CHECKING 추가

from flask import Flask, abort, jsonify, render_template, request, url_for

# Vercel 배포 시, template_folder 경로를 상위 폴더로 변경해야 함.
app = Flask(__name__) 

# 환경 변수에서 ADMIN_KEY를 가져옵니다. 설정되지 않았다면 "changeme" 사용.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = 1000 # 기록 최대 길이

# 타입 힌트 단순화를 위해 Dict[str, Any]를 SessionDict로 정의
SessionDict = Dict[str, Any]
# 메모리 내 공유 세션 저장소 (실서비스에서는 DB/캐시 사용 권장)
sessions: Dict[str, SessionDict] = {}


def _get_session(token: str) -> Dict[str, Any]:
    """토큰을 사용하여 세션을 조회하고, 없으면 404 오류 발생"""
    session = sessions.get(token)
    if session is None:
        abort(404, description="Unknown share token")
    return session


@app.get("/")
def index():
    """새 세션 생성을 위한 시작 페이지"""
    return render_template("index.html")


@app.post("/api/session")
def create_session():
    """새로운 위치 공유 세션을 생성하고 토큰 및 URL 반환"""
    token = secrets.token_urlsafe(8)
    # _external=True는 외부에서 접근 가능한 전체 URL을 생성 (Vercel 배포 시 필수)
    track_url = url_for("track_page", token=token, _external=True) 
    
    sessions[token] = {
        "latest": None,
        "history": deque(maxlen=MAX_HISTORY), # Deque를 사용하여 최대 기록 수 제한
        "track_url": track_url,
    }
    return (
        jsonify(
            {
                "token": token,
                "share_url": url_for("share_page", token=token, _external=True),
                "track_url": track_url, # 트랙 URL도 함께 반환
            }
        ),
        201,
    )


@app.get("/share/<token>")
def share_page(token: str):
    """상대방이 위치 공유를 허용하는 페이지"""
    _get_session(token)
    return render_template("share.html", token=token)


@app.post("/api/location/<token>")
def update_location(token: str):
    """[핵심] 상대방으로부터 위치 데이터를 수신 및 저장"""
    session = _get_session(token)
    payload = request.get_json(silent=True) or {}
    lat = payload.get("lat")
    lng = payload.get("lng")

    if lat is None or lng is None:
        abort(400, description="lat/lng is required")

    # 위치 정보 스냅샷 생성
    snapshot = {
        "lat": float(lat),
        "lng": float(lng),
        "accuracy": payload.get("accuracy"),
        "heading": payload.get("heading"),
        "speed": payload.get("speed"),
        "captured_at": datetime.now(timezone.utc).isoformat(), # UTC 시간으로 기록
    }
    session["latest"] = snapshot
    history: Deque[Dict[str, Any]] = session["history"]
    history.append(snapshot)
    return jsonify({"status": "ok"})


@app.get("/api/location/<token>")
def latest_location(token: str):
    """뷰어 페이지에서 사용할 최신 위치 데이터 조회"""
    session = _get_session(token)
    latest: Optional[Dict[str, Any]] = session.get("latest")
    if latest is None:
        return jsonify({"available": False})
    return jsonify({"available": True, "location": latest})


@app.get("/track/<token>")
def track_page(token: str):
    """위치 확인 (지도 뷰어) 페이지"""
    _get_session(token)
    return render_template("track.html", token=token)


@app.get("/admin")
def admin_sessions():
    """관리자 페이지 (ADMIN_KEY 필요)"""
    key = request.args.get("key")
    if key != ADMIN_KEY:
        # 키가 일치하지 않으면 접근 거부
        abort(403, description="Forbidden") 
    
    # ... (관리자 페이지 로직 생략 없이 그대로 유지)

    token_filter = request.args.get("token")
    items = [
        {
            "token": token,
            "share_url": url_for("share_page", token=token, _external=True),
            "track_url": data.get("track_url"),
            "has_location": data.get("latest") is not None,
            "count": len(data.get("history", [])),
        }
        for token, data in sessions.items()
    ]
    items.sort(key=lambda item: item["token"])

    selected_history = []
    selected_token = None
    if token_filter:
        target = sessions.get(token_filter)
        if target:
            selected_token = token_filter
            selected_history = list(reversed(target["history"]))

    return render_template(
        "admin.html",
        sessions=items,
        selected_token=selected_token,
        history=selected_history,
        max_history=MAX_HISTORY,
    )


if __name__ == "__main__":
    # 로컬 테스트를 위해 8888 포트에서 실행
    print(f"ADMIN_KEY: {ADMIN_KEY}")
    app.run(debug=True, host="0.0.0.0", port=8888)