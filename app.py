# app.py

from __future__ import annotations

import os
from dotenv import load_dotenv 

# .env 파일을 읽어 환경 변수를 로드합니다. (로컬 실행 시 필요)
load_dotenv() 

import secrets
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, Optional
import atexit 

from flask import Flask, abort, jsonify, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy # SQLAlchemy 임포트
from apscheduler.schedulers.background import BackgroundScheduler 

# ----------------------------------------------------
# ⚙️ 환경 변수 및 전역 설정
# ----------------------------------------------------

# 환경 변수에서 값 로드
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///site.db") # SQLite DB 파일 경로
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1000)) # DB 사용 시에도 기록 수 제한에 사용
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 24))


app = Flask(__name__) 

# DB 설정
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app) # SQLAlchemy 초기화


# ----------------------------------------------------
# 📚 데이터베이스 모델 정의
# ----------------------------------------------------

# UTC 시간을 DB에 저장할 때 사용
def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Session(db.Model):
    __tablename__ = 'sessions'
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    
    # 최신 위치 정보 (DB에서 직접 쿼리하지 않도록 Session 테이블에 캐싱)
    latest_lat = db.Column(db.Float)
    latest_lng = db.Column(db.Float)
    latest_accuracy = db.Column(db.Float)
    latest_heading = db.Column(db.Float)
    latest_speed = db.Column(db.Float)
    latest_captured_at = db.Column(db.DateTime) 
    
    # 세션과 위치 기록을 1:N 관계로 연결 (세션 삭제 시 기록도 함께 삭제)
    history = db.relationship('LocationHistory', backref='session', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Session {self.token}>'

class LocationHistory(db.Model):
    __tablename__ = 'location_history'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id'), nullable=False)
    
    lat = db.Column(db.Float, nullable=False)
    lng = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Float)
    heading = db.Column(db.Float)
    speed = db.Column(db.Float)
    captured_at = db.Column(db.DateTime, default=now_utc) 

    def __repr__(self):
        return f'<Location {self.session_id} at {self.captured_at}>'


# ----------------------------------------------------
# 🚀 애플리케이션 시작 시 DB 파일 및 테이블 생성
# ----------------------------------------------------

with app.app_context():
    # 이 코드가 실행되면 site.db 파일과 테이블이 생성/업데이트됩니다.
    db.create_all() 
    print("데이터베이스 초기화 완료 (site.db)")


# ----------------------------------------------------
# 헬퍼 함수
# ----------------------------------------------------

def _get_session(token: str) -> Session:
    """토큰을 사용하여 세션을 DB에서 조회하고, 없으면 404 오류 발생"""
    # Session.query.get(token)은 primary key만 조회하므로 filter_by 사용
    session = Session.query.filter_by(token=token).first()
    if session is None:
        abort(404, description="Unknown share token")
    return session


# ----------------------------------------------------
# 🧹 세션 정리(Cleanup) 로직 (APScheduler Job)
# ----------------------------------------------------

def cleanup_expired_sessions():
    """만료된 세션 및 관련 위치 기록을 DB에서 정리합니다."""
    
    with app.app_context():
        # 만료 기준 시각 계산
        expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)

        # 30일보다 오래된 세션을 쿼리하여 삭제
        sessions_to_delete = Session.query.filter(Session.created_at < expiration_time).all()
        
        count = len(sessions_to_delete)
        for s in sessions_to_delete:
            db.session.delete(s)
        
        db.session.commit()
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {count}개의 만료된 세션 정리 완료 (기준: {MAX_SESSION_LIFETIME_HOURS}시간)")


# ----------------------------------------------------
# ⏰ 스케줄러 설정 및 시작
# ----------------------------------------------------

scheduler = BackgroundScheduler()
# 30분마다 cleanup_expired_sessions 함수 실행
scheduler.add_job(func=cleanup_expired_sessions, trigger="interval", minutes=30)
scheduler.start()
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
    
    token = secrets.token_hex(16) # 32자리 16진수 토큰 (secrets.token_urlsafe(8) 대신 사용)
    track_url = url_for("track_page", token=token, _external=True) 
    
    new_session = Session(token=token)
    db.session.add(new_session)
    db.session.commit()

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
        
    current_time = now_utc()
    
    # 1. 새 위치 기록 생성
    new_location = LocationHistory(
        session_id=session.id,
        lat=float(lat),
        lng=float(lng),
        accuracy=payload.get("accuracy"),
        heading=payload.get("heading"),
        speed=payload.get("speed"),
        captured_at=current_time
    )
    db.session.add(new_location)
    
    # 2. Session 테이블에 최신 위치 정보 캐싱 (조회 성능 향상)
    session.latest_lat = new_location.lat
    session.latest_lng = new_location.lng
    session.latest_accuracy = new_location.accuracy
    session.latest_heading = new_location.heading
    session.latest_speed = new_location.speed
    session.latest_captured_at = new_location.captured_at
    
    # 3. 최대 기록 수 초과 시 가장 오래된 기록 삭제 (FIFO)
    # Deque 대신 DB에서 직접 처리
    current_count = session.history.count()
    if current_count > MAX_HISTORY:
        oldest_history = session.history.order_by(LocationHistory.captured_at.asc()).first()
        if oldest_history:
            db.session.delete(oldest_history)

    db.session.commit()
    return jsonify({"status": "ok"})


@app.get("/api/location/<token>")
def latest_location(token: str):
    """뷰어 페이지에서 사용할 최신 위치 데이터 조회 (캐싱된 데이터 사용)"""
    session = _get_session(token)
    
    if session.latest_lat is None:
        return jsonify({"available": False})
        
    # latest 필드를 DB의 캐싱된 데이터로 구성
    latest = {
        "lat": session.latest_lat,
        "lng": session.latest_lng,
        "accuracy": session.latest_accuracy,
        "heading": session.latest_heading,
        "speed": session.latest_speed,
        "captured_at": session.latest_captured_at.replace(tzinfo=timezone.utc).isoformat() if session.latest_captured_at else None,
    }
    
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
    
    # 모든 세션 불러오기 (최신 생성 순)
    all_sessions = Session.query.order_by(Session.created_at.desc()).all()

    items = []
    for s in all_sessions:
        items.append({
            "token": s.token,
            "share_url": url_for("share_page", token=s.token, _external=True),
            "track_url": url_for("track_page", token=s.token, _external=True),
            "has_location": s.latest_lat is not None, # latest_lat이 있으면 위치가 있는 것으로 판단
            "count": s.history.count(), # DB 쿼리를 통해 기록 수 계산
        })

    selected_history = []
    selected_token = None
    if token_filter:
        target_session = Session.query.filter_by(token=token_filter).first()
        if target_session:
            selected_token = token_filter
            # 해당 세션의 위치 기록을 MAX_HISTORY 개만큼 최신 순으로 조회
            history_query = target_session.history.order_by(LocationHistory.captured_at.desc())
            
            selected_history = [
                {
                    'lat': h.lat,
                    'lng': h.lng,
                    'accuracy': h.accuracy,
                    'heading': h.heading,
                    'speed': h.speed,
                    # 타임존 정보 없이 저장했으므로, KST로 변환하여 출력 (선택 사항)
                    'captured_at': (h.captured_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
                }
                for h in history_query.limit(MAX_HISTORY).all()
            ]

    return render_template(
        "admin.html",
        sessions=items,
        selected_token=selected_token,
        history=selected_history,
        max_history=MAX_HISTORY,
    )


if __name__ == "__main__":
    print(f"ADMIN_KEY: {ADMIN_KEY}")
    print(f"DATABASE: {DATABASE_URL}")
    print(f"MAX_SESSION_LIFETIME_HOURS: {MAX_SESSION_LIFETIME_HOURS}시간")
    print("APScheduler가 백그라운드에서 실행 중입니다...")
    # use_reloader=False: 디버그 모드에서 APscheduler가 두 번 실행되는 것을 방지
    app.run(debug=True, host="0.0.0.0", port=8888, use_reloader=False)
