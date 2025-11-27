# app.py

from __future__ import annotations

import os
from dotenv import load_dotenv 

# .env 파일을 읽어 환경 변수를 로드합니다.
load_dotenv() 

import secrets
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, Optional
import atexit # 애플리케이션 종료 시 스케줄러를 종료하기 위해 추가

from flask import Flask, abort, jsonify, render_template, request, url_for
from apscheduler.schedulers.background import BackgroundScheduler # 스케줄러 추가

# Vercel 배포 시, template_folder 경로를 상위 폴더로 변경해야 함.
app = Flask(__name__) 

# ----------------------------------------------------
# ⚙️ 환경 변수 및 전역 설정
# ----------------------------------------------------

# 환경 변수에서 ADMIN_KEY를 가져옵니다.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
# 환경 변수에서 MAX_HISTORY를 가져옵니다. (기본값: 1000)
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1000)) 
# 세션 만료 기간 (시간 단위). (기본값: 24시간)
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 24))


# 타입 힌트 단순화를 위해 Dict[str, Any]를 SessionDict로 정의
SessionDict = Dict[str, Any]
# 메모리 내 공유 세션 저장소
sessions: Dict[str, SessionDict] = {}


def _get_session(token: str) -> Dict[str, Any]:
    """토큰을 사용하여 세션을 조회하고, 없으면 404 오류 발생"""
    session = sessions.get(token)
    if session is None:
        abort(404, description="Unknown share token")
    return session

# ----------------------------------------------------
# 🧹 세션 정리(Cleanup) 로직 (APScheduler Job)
# ----------------------------------------------------

def cleanup_expired_sessions():
    """만료된 세션을 메모리에서 정리합니다."""
    
    # 만료 기준 시각 계산 (현재 시각 - 세션 수명)
    expiration_time = datetime.now(timezone.utc) - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
    
    tokens_to_delete = []
    
    # 'sessions' 딕셔너리를 순회하며 만료된 세션 찾기
    for token, data in sessions.items():
        # 세션 생성 시각이 만료 기준 시각보다 이전이면 삭제 대상으로 지정
        created_at_str = data.get("created_at")
        if created_at_str:
            created_at = datetime.fromisoformat(created_at_str)
            if created_at < expiration_time:
                tokens_to_delete.append(token)

    # 정리
    for token in tokens_to_delete:
        del sessions[token]
        
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {len(tokens_to_delete)}개의 만료된 세션 정리 완료 (만료 기준: {MAX_SESSION_LIFETIME_HOURS}시간)")


# ----------------------------------------------------
# 🚀 스케줄러 설정 및 시작
# ----------------------------------------------------

# 백그라운드 스케줄러 인스턴스 생성
scheduler = BackgroundScheduler()

# cleanup_expired_sessions 함수를 매 30분마다 실행하도록 설정
# cron 트리거 대신 interval 트리거를 사용하여 설정의 단순성을 높였습니다.
scheduler.add_job(func=cleanup_expired_sessions, trigger="interval", minutes=30)
scheduler.start()

# 애플리케이션 종료 시 스케줄러를 안전하게 종료하도록 설정
atexit.register(lambda: scheduler.shutdown())


# ----------------------------------------------------
# 🗺️ 경로 (Routes) 정의
# ----------------------------------------------------

@app.get("/")
def index():
    """새 세션 생성을 위한 시작 페이지"""
    return render_template("index.html")


@app.post("/api/session")
def create_session():
    """새로운 위치 공유 세션을 생성하고 토큰 및 URL 반환"""
    token = secrets.token_urlsafe(8)
    track_url = url_for("track_page", token=token, _external=True) 
    
    # created_at 필드를 추가하여 만료 기간을 계산할 수 있도록 함
    sessions[token] = {
        "created_at": datetime.now(timezone.utc).isoformat(), # UTC 시간으로 생성 시각 기록
        "latest": None,
        "history": deque(maxlen=MAX_HISTORY),
        "track_url": track_url,
    }
    return (
        jsonify(
            {
                "token": token,
                "share_url": url_for("share_page", token=token, _external=True),
                "track_url": track_url,
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
        abort(403, description="Forbidden") 
    
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
    # 세션 생성 시각을 기준으로 정렬 (최신 순)
    items.sort(
        key=lambda item: datetime.fromisoformat(sessions[item['token']].get("created_at", datetime.min.isoformat())), 
        reverse=True
    )

    selected_history = []
    selected_token = None
    if token_filter:
        target = sessions.get(token_filter)
        if target:
            selected_token = token_filter
            # 기록은 최신 순으로 표시하기 위해 역순으로 변환
            selected_history = list(reversed(target["history"]))

    return render_template(
        "admin.html",
        sessions=items,
        selected_token=selected_token,
        history=selected_history,
        max_history=MAX_HISTORY,
    )


if __name__ == "__main__":
    print(f"ADMIN_KEY: {ADMIN_KEY}")
    print(f"MAX_HISTORY: {MAX_HISTORY}")
    print(f"MAX_SESSION_LIFETIME_HOURS: {MAX_SESSION_LIFETIME_HOURS}시간")
    print("APScheduler가 백그라운드에서 실행 중입니다...")
    app.run(debug=True, host="0.0.0.0", port=8888, use_reloader=False) 
