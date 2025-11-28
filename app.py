# app.py (Vercel 호환성 최적화 버전)

from __future__ import annotations

import os
from dotenv import load_dotenv 
from pathlib import Path 

# .env 파일을 읽어 환경 변수를 로드합니다. (로컬 실행 시 필요)
load_dotenv() 

import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from flask import Flask, abort, jsonify, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy 

# ----------------------------------------------------
# ⚙️ 환경 변수 및 전역 설정
# ----------------------------------------------------

# Vercel 환경 감지 및 DB 경로 설정
if os.getenv('VERCEL') == '1' or os.getenv('VERCEL_ENV'):
    # Vercel 환경: /tmp 디렉토리에 DB 파일 생성 (데이터 영속성은 낮음)
    DB_FILE_PATH = Path('/tmp') / 'site.db'
    DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"
    print(f"INFO: Vercel detected. Using temporary path: {DATABASE_URL}")
else:
    # 로컬 환경: .env 또는 기본 경로 사용
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///site.db")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1000)) 
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 24))


app = Flask(__name__) 

# DB 설정
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app) 


# ----------------------------------------------------
# 📚 데이터베이스 모델 정의
# ----------------------------------------------------

def now_utc():
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
# 🔪 DB 초기화 코드를 Flask CLI 명령으로 변경 (임포트 에러 방지)
# ----------------------------------------------------

@app.cli.command("init-db")
def init_db():
    """DB 파일 및 테이블 생성 (Vercel에서 첫 배포 시 실행 필요)"""
    db.create_all()
    print("DB 테이블이 성공적으로 초기화되었습니다.")

# ----------------------------------------------------
# 🧹 만료 세션 정리 로직 (API 엔드포인트로 변경)
# ----------------------------------------------------

def cleanup_expired_sessions():
    """만료된 세션 및 관련 위치 기록을 DB에서 정리합니다."""
    # Vercel은 스케줄러를 지원하지 않으므로, 이 함수를 외부 Cron 서비스나
    # 관리자 접근 시 호출하는 방식으로 변경해야 합니다.
    
    expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
    sessions_to_delete = Session.query.filter(Session.created_at < expiration_time).all()
    
    count = len(sessions_to_delete)
    for s in sessions_to_delete:
        db.session.delete(s)
    
    db.session.commit()
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {count}개의 만료된 세션 정리 완료.")
    return count

# 관리자 키를 가진 사용자가 수동으로 실행할 수 있는 엔드포인트 추가
@app.post("/api/admin/cleanup")
def run_cleanup():
    key = request.args.get("key")
    if key != ADMIN_KEY:
        abort(403, description="Forbidden")
    
    count = cleanup_expired_sessions()
    return jsonify({"status": "ok", "deleted_count": count})


# ----------------------------------------------------
# 헬퍼 함수 및 경로 (Routes) 정의 (기존과 동일)
# ----------------------------------------------------

def _get_session(token: str) -> Session:
    session = Session.query.filter_by(token=token).first()
    if session is None:
        abort(404, description="Unknown share token")
    return session

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
    if lat is None or lng is None: abort(400, description="lat/lng is required")
        
    current_time = now_utc()
    new_location = LocationHistory(
        session_id=session.id, lat=float(lat), lng=float(lng), accuracy=payload.get("accuracy"), 
        heading=payload.get("heading"), speed=payload.get("speed"), captured_at=current_time
    )
    db.session.add(new_location)
    session.latest_lat = new_location.lat
    session.latest_lng = new_location.lng
    session.latest_accuracy = new_location.accuracy
    session.latest_heading = new_location.heading
    session.latest_speed = new_location.speed
    session.latest_captured_at = new_location.captured_at
    
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
    if session.latest_lat is None: return jsonify({"available": False})
        
    latest = {
        "lat": session.latest_lat, "lng": session.latest_lng, "accuracy": session.latest_accuracy,
        "heading": session.latest_heading, "speed": session.latest_speed,
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
    if key != ADMIN_KEY: abort(403, description="Forbidden") 
    
    token_filter = request.args.get("token")
    all_sessions = Session.query.order_by(Session.created_at.desc()).all()

    items = []
    for s in all_sessions:
        items.append({
            "token": s.token, "share_url": url_for("share_page", token=s.token, _external=True),
            "track_url": url_for("track_page", token=s.token, _external=True),
            "has_location": s.latest_lat is not None, "count": s.history.count(), 
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
                    'lat': h.lat, 'lng': h.lng, 'accuracy': h.accuracy, 'heading': h.heading, 'speed': h.speed,
                    'captured_at': (h.captured_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
                }
                for h in history_query.limit(MAX_HISTORY).all()
            ]

    return render_template("admin.html", sessions=items, selected_token=selected_token, history=selected_history, max_history=MAX_HISTORY)


if __name__ == "__main__":
    print(f"ADMIN_KEY: {ADMIN_KEY}")
    print(f"DATABASE: {DATABASE_URL}")
    print(f"MAX_SESSION_LIFETIME_HOURS: {MAX_SESSION_LIFETIME_HOURS}시간")
    print("WARNING: Background cleanup will not run in local debug mode (use Flask CLI init-db).")
    # 로컬에서는 debug 모드로 실행 (Vercel에서는 이 부분이 실행되지 않음)
    app.run(debug=True, host="0.0.0.0", port=8888)
