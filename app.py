# app.py (Vercel 호환성 강화 버전)

from __future__ import annotations

import os
from dotenv import load_dotenv 
from pathlib import Path # 경로 처리를 위해 추가

# .env 파일을 읽어 환경 변수를 로드합니다. (로컬 실행 시 필요)
load_dotenv() 

import secrets
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, Optional
import atexit 

from flask import Flask, abort, jsonify, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy 
from apscheduler.schedulers.background import BackgroundScheduler 

# ----------------------------------------------------
# ⚙️ 환경 변수 및 전역 설정
# ----------------------------------------------------

# Vercel 환경 감지 및 DB 경로 설정 수정
if os.getenv('VERCEL') == '1' or os.getenv('VERCEL_ENV'):
    # Vercel 환경: 쓰기가 가능한 /tmp 디렉토리에 DB 파일을 생성
    DB_FILE_PATH = Path('/tmp') / 'site.db'
    DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"
    print(f"INFO: Vercel detected. Using temporary path: {DATABASE_URL}")
else:
    # 로컬 환경: .env 또는 기본 경로 사용
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///site.db")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1000)) 
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 152233600))


app = Flask(__name__) 

# DB 설정
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app) 


# ----------------------------------------------------
# 📚 데이터베이스 모델 정의 (변경 없음)
# ----------------------------------------------------

# UTC 시간을 DB에 저장할 때 사용
def now_utc():
    # SQLite는 타임존 정보를 지원하지 않으므로, naive datetime 객체로 변환하여 저장
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
    # Vercel에서 /tmp 경로를 사용하더라도 테이블이 확실히 생성되도록 보장
    db.create_all() 
    print("데이터베이스 초기화 완료 (site.db)")


# ----------------------------------------------------
# 헬퍼 함수, 정리 로직, 스케줄러 (변경 없음)
# ----------------------------------------------------

def _check_and_cleanup_expired_session(session: Session) -> bool:
    """세션이 만료되었는지 확인하고, 만료되었으면 삭제. True면 만료됨, False면 유효함"""
    if session.created_at:
        expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
        if session.created_at < expiration_time:
            db.session.delete(session)
            db.session.commit()
            return True
    return False

def _get_session(token: str) -> Session:
    session = Session.query.filter_by(token=token).first()
    if session is None:
        abort(404, description="Unknown share token")
    
    # 세션 만료 체크 및 자동 삭제
    if _check_and_cleanup_expired_session(session):
        abort(404, description="Session expired")
    
    return session

def cleanup_expired_sessions():
    with app.app_context():
        expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
        sessions_to_delete = Session.query.filter(Session.created_at < expiration_time).all()
        
        count = len(sessions_to_delete)
        for s in sessions_to_delete:
            db.session.delete(s)
        
        db.session.commit()
        
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {count}개의 만료된 세션 정리 완료 (기준: {MAX_SESSION_LIFETIME_HOURS}시간)")

# Vercel 환경에서는 스케줄러를 사용하지 않고, 요청 기반 lazy cleanup을 사용합니다.
# 로컬 환경에서만 스케줄러를 활성화합니다.
if not (os.getenv('VERCEL') == '1' or os.getenv('VERCEL_ENV')):
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=cleanup_expired_sessions, trigger="interval", minutes=30)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("APScheduler가 백그라운드에서 실행 중입니다...")
else:
    print("INFO: Vercel 환경 감지 - 스케줄러 비활성화, 요청 기반 정리 사용")


# ----------------------------------------------------
# 🗺️ 경로 (Routes) 정의 (데이터 처리 로직 변경 없음)
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
    _get_session(token)
    return render_template("track.html", token=token)


@app.get("/admin")
def admin_sessions():
    key = request.args.get("key")
    if key != ADMIN_KEY:
        abort(403, description="Forbidden") 
    
    # 만료된 세션 자동 정리
    expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
    expired_sessions = Session.query.filter(Session.created_at < expiration_time).all()
    for s in expired_sessions:
        db.session.delete(s)
    if expired_sessions:
        db.session.commit()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {len(expired_sessions)}개의 만료된 세션 정리 완료 (기준: {MAX_SESSION_LIFETIME_HOURS}시간)")
    
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
