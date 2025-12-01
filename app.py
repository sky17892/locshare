from __future__ import annotations

import os
from dotenv import load_dotenv 
from pathlib import Path 
import secrets
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, Optional
import atexit 

from flask import Flask, abort, jsonify, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy 
from apscheduler.schedulers.background import BackgroundScheduler 

# .env 파일을 읽어 환경 변수를 로드합니다. (로컬 실행 시 필요)
load_dotenv() 

# ----------------------------------------------------
# ⚙️ 환경 변수 및 전역 설정 (MySQL 연동 부분)
# ----------------------------------------------------

# PHP 파일에서 가져온 MySQL DB 정보
# 🚨 수정: 포트 번호(:3306)를 제거하고 호스트 주소만 남겼습니다.
MYSQL_HOST = 'sky16015.dothome.co.kr'
MYSQL_USER = 'sky16015'
MYSQL_PASSWORD = 'sky02564!'
MYSQL_DB = 'sky16015'

# Flask-SQLAlchemy용 MySQL 연결 URL 생성 (PyMySQL 드라이버 사용)
# 형식: mysql+pymysql://<user>:<password>@<host>/<dbname>?charset=utf8mb4
# 🚨 수정: 인코딩 오류 방지를 위해 '?charset=utf8mb4'를 다시 추가했습니다.
FALLBACK_DATABASE_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/{MYSQL_DB}?charset=utf8mb4"
)

# Vercel 환경 변수 'DATABASE_URL'을 우선 사용하고, 없으면 위 MySQL 정보를 사용합니다.
DATABASE_URL = os.environ.get("DATABASE_URL", FALLBACK_DATABASE_URL)


ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1500)) 
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 8760000)) 

# Vercel 환경 감지 및 DB 경로 출력
if os.getenv('VERCEL') == '1' or os.getenv('VERCEL_ENV'):
    print(f"INFO: Vercel detected. Using external database URL.")
else:
    print(f"INFO: Local environment. Using DATABASE_URL: {DATABASE_URL}")

app = Flask(__name__) 

# DB 설정
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# MySQL 연결 시 인코딩 및 연결 끊김 방지 설정 추가 (charset은 URL에 있으므로 제외)
if DATABASE_URL.startswith("mysql"):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_recycle': 280,  # MySQL 연결 끊김 방지
    }

db = SQLAlchemy(app) 


# ----------------------------------------------------
# 📚 데이터베이스 모델 정의 (변경 없음)
# ----------------------------------------------------

# UTC 시간을 DB에 저장할 때 사용
def now_utc():
    # 타임존 정보가 없는 naive datetime 객체로 저장
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Session(db.Model):
    __tablename__ = 'sessions'
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    latest_lat = db.Column(db.Float)
    latest_lng = db.Column(db.Float)
    latest_accuracy = db.Column(db.Float)
    latest_heading = db.Column(db.Float)
    latest_speed = db.Column(db.Float)
    latest_captured_at = db.Column(db.DateTime) 
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
    # MySQL 환경에서 테이블이 없으면 생성되도록 보장
    db.create_all() 
    print(f"데이터베이스 초기화 완료 (DB Type: {'MySQL' if DATABASE_URL.startswith('mysql') else 'SQLite'})")


# ----------------------------------------------------
# 헬퍼 함수, 정리 로직, 스케줄러 (변경 없음)
# ----------------------------------------------------

def _get_session(token: str) -> Session:
    session = Session.query.filter_by(token=token).first()
    if session is None:
        abort(404, description="Unknown share token")
    return session

def cleanup_expired_sessions():
    with app.app_context():
        # UTC를 기준으로 만료 시간 계산
        expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
        sessions_to_delete = Session.query.filter(Session.created_at < expiration_time).all()
        
        count = len(sessions_to_delete)
        for s in sessions_to_delete:
            db.session.delete(s)
        
        db.session.commit()
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {count}개의 만료된 세션 정리 완료 (기준: {MAX_SESSION_LIFETIME_HOURS}시간)")

scheduler = BackgroundScheduler()
# Vercel에서는 작동하지 않지만 로컬 테스트를 위해 유지
scheduler.add_job(func=cleanup_expired_sessions, trigger="interval", minutes=30)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# ----------------------------------------------------
# 🗺️ 경로 (Routes) 정의 (변경 없음)
# ----------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/session")
def create_session():
    token = secrets.token_hex(16) 
    track_url = url_for("track_page", token=token, _external=True) 
    new_session = Session(token=token)
    db.session.add(new_session)
    db.session.commit()
    return ( jsonify({"token": token, "share_url": url_for("share_page", token=token, _external=True), "track_url": track_url,}), 201,)


@app.get("/share/<token>")
def share_page(token: str):
    _get_session(token)
    return render_template("share.html", token=token)


@app.post("/api/location/<token>")
def update_location(token: str):
    session = _get_session(token)
    payload = request.get_json(silent=True) or {}
    lat = payload.get("lat")
    lng = payload.get("lng")

    if lat is None or lng is None:
        abort(400, description="lat/lng is required")
        
    current_time = now_utc()
    
    # 1. 새 위치 기록 생성
    new_location = LocationHistory(
        session_id=session.id, lat=float(lat), lng=float(lng), accuracy=payload.get("accuracy"), 
        heading=payload.get("heading"), speed=payload.get("speed"), captured_at=current_time
    )
    db.session.add(new_location)
    
    # 2. Session 테이블에 최신 위치 정보 캐싱
    session.latest_lat = new_location.lat
    session.latest_lng = new_location.lng
    session.latest_accuracy = new_location.accuracy
    session.latest_heading = new_location.heading
    session.latest_speed = new_location.speed
    session.latest_captured_at = new_location.captured_at
    
    # 3. 최대 기록 수 초과 시 가장 오래된 기록 삭제 (FIFO)
    current_count = session.history.count()
    if current_count > MAX_HISTORY:
        oldest_history = session.history.order_by(LocationHistory.captured_at.asc()).first()
        if oldest_history:
            db.session.delete(oldest_history)

    db.session.commit()
    return jsonify({"status": "ok"})


@app.get("/api/location/<token>")
def latest_location(token: str):
    session = _get_session(token)
    
    if session.latest_lat is None:
        return jsonify({"available": False})
        
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
    session = _get_session(token)
    # 세션 정보를 템플릿에 전달
    session_info = {
        "token": session.token,
        # DB의 UTC 시간에 한국 시간(KST, UTC+9)을 적용하여 출력
        "created_at": (session.created_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S') if session.created_at else None,
        "has_location": session.latest_lat is not None,
        "count": session.history.count(),
        "max_history": MAX_HISTORY,
    }
    return render_template("track.html", token=token, session_info=session_info)

@app.get("/api/session/<token>/history")
def get_session_history(token: str):
    """세션 기록을 가져오는 API"""
    session = _get_session(token)
    history_query = session.history.order_by(LocationHistory.captured_at.desc())
    
    history = [
        {
            'lat': h.lat,
            'lng': h.lng,
            'accuracy': h.accuracy,
            'heading': h.heading,
            'speed': h.speed,
            # DB의 UTC 시간에 한국 시간(KST, UTC+9)을 적용하여 출력
            'captured_at': (h.captured_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S') if h.captured_at else None
        }
        for h in history_query.limit(MAX_HISTORY).all()
    ]
    
    return jsonify({
        "token": session.token,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "has_location": session.latest_lat is not None,
        "count": len(history),
        "history": history
    })


@app.get("/admin")
def admin_sessions():
    key = request.args.get("key")
    if key != ADMIN_KEY:
        abort(403, description="Forbidden") 
    
    token_filter = request.args.get("token")
    all_sessions = Session.query.order_by(Session.created_at.desc()).all()

    items = []
    for s in all_sessions:
        items.append({
            "token": s.token,
            "share_url": url_for("share_page", token=s.token, _external=True),
            "track_url": url_for("track_page", token=s.token, _external=True),
            "has_location": s.latest_lat is not None, 
            "count": s.history.count(), 
        })

    selected_history = []
    selected_token = None
    if token_filter:
        target_session = Session.query.filter_by(token=token_filter).first()
        if target_session:
            selected_token = token_filter
            history_query = target_session.history.order_by(LocationHistory.captured_at.desc())
            
            selected_history = [
                {
                    'lat': h.lat,
                    'lng': h.lng,
                    'accuracy': h.accuracy,
                    'heading': h.heading,
                    'speed': h.speed,
                    # DB의 UTC 시간에 한국 시간(KST, UTC+9)을 적용하여 출력
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
        max_session_lifetime_hours=MAX_SESSION_LIFETIME_HOURS,
    )


if __name__ == "__main__":
    print(f"ADMIN_KEY: {ADMIN_KEY}")
    print(f"DATABASE: {DATABASE_URL}")
    print(f"MAX_SESSION_LIFETIME_HOURS: {MAX_SESSION_LIFETIME_HOURS}시간")
    print("APScheduler가 백그라운드에서 실행 중입니다...")
    app.run(debug=True, host="0.0.0.0", port=8888, use_reloader=False)
