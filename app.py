from __future__ import annotations

import os
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import secrets
import atexit

from flask import Flask, abort, jsonify, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler

# ----------------------------------------------------
# ⚙️ 환경 변수 로드
# ----------------------------------------------------
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 1000))
MAX_SESSION_LIFETIME_HOURS = int(os.environ.get("MAX_SESSION_LIFETIME_HOURS", 8760000))

# ----------------------------------------------------
# 🌐 Flask & SQLAlchemy
# ----------------------------------------------------
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Aiven MySQL SSL 옵션 추가
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "connect_args": {
        "ssl": {"ssl": True}
    }
}

db = SQLAlchemy(app)

# ----------------------------------------------------
# 📚 모델 정의
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
    history = db.relationship(
        'LocationHistory',
        backref='session',
        lazy='dynamic',
        cascade="all, delete-orphan"
    )

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

# ----------------------------------------------------
# 🗂️ DB 생성
# ----------------------------------------------------
with app.app_context():
    db.create_all()
    print("[INFO] Database initialized.")

# ----------------------------------------------------
# 🧹 만료 세션 자동 삭제
# ----------------------------------------------------
def cleanup_expired_sessions():
    with app.app_context():
        expiration_time = datetime.utcnow() - timedelta(hours=MAX_SESSION_LIFETIME_HOURS)
        sessions_to_delete = Session.query.filter(Session.created_at < expiration_time).all()

        count = len(sessions_to_delete)
        for s in sessions_to_delete:
            db.session.delete(s)

        db.session.commit()
        print(f"[{datetime.now()}] {count} expired sessions cleaned.")

scheduler = BackgroundScheduler()
scheduler.add_job(func=cleanup_expired_sessions, trigger="interval", minutes=30)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

# ----------------------------------------------------
# 🧩 헬퍼
# ----------------------------------------------------
def _get_session(token: str) -> Session:
    session = Session.query.filter_by(token=token).first()
    if session is None:
        abort(404, "Unknown share token")
    return session

# ----------------------------------------------------
# 🌍 Routes
# ----------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/session")
def create_session():
    token = secrets.token_hex(16)
    new_session = Session(token=token)
    db.session.add(new_session)
    db.session.commit()

    return (
        jsonify({
            "token": token,
            "share_url": url_for("share_page", token=token, _external=True),
            "track_url": url_for("track_page", token=token, _external=True),
        }),
        201,
    )

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
        abort(400, "lat/lng is required")

    current_time = now_utc()

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

    session.latest_lat = new_location.lat
    session.latest_lng = new_location.lng
    session.latest_accuracy = new_location.accuracy
    session.latest_heading = new_location.heading
    session.latest_speed = new_location.speed
    session.latest_captured_at = new_location.captured_at

    current_count = session.history.count()
    if current_count > MAX_HISTORY:
        oldest = session.history.order_by(LocationHistory.captured_at.asc()).first()
        if oldest:
            db.session.delete(oldest)

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
        "captured_at": session.latest_captured_at.replace(tzinfo=timezone.utc).isoformat()
        if session.latest_captured_at else None,
    }

    return jsonify({"available": True, "location": latest})

@app.get("/track/<token>")
def track_page(token: str):
    session = _get_session(token)
    session_info = {
        "token": session.token,
        "created_at": (session.created_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
        "has_location": session.latest_lat is not None,
        "count": session.history.count(),
        "max_history": MAX_HISTORY,
    }
    return render_template("track.html", token=token, session_info=session_info)

@app.get("/api/session/<token>/history")
def get_session_history(token: str):
    session = _get_session(token)

    history_query = session.history.order_by(LocationHistory.captured_at.desc())
    history = [
        {
            "lat": h.lat,
            "lng": h.lng,
            "accuracy": h.accuracy,
            "heading": h.heading,
            "speed": h.speed,
            "captured_at": (h.captured_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
        }
        for h in history_query.limit(MAX_HISTORY).all()
    ]

    return jsonify({
        "token": session.token,
        "created_at": session.created_at.isoformat(),
        "has_location": session.latest_lat is not None,
        "count": len(history),
        "history": history
    })

@app.get("/admin")
def admin_sessions():
    key = request.args.get("key")
    if key != ADMIN_KEY:
        abort(403)

    token_filter = request.args.get("token")
    all_sessions = Session.query.order_by(Session.created_at.desc()).all()

    items = [
        {
            "token": s.token,
            "share_url": url_for("share_page", token=s.token, _external=True),
            "track_url": url_for("track_page", token=s.token, _external=True),
            "has_location": s.latest_lat is not None,
            "count": s.history.count(),
        }
        for s in all_sessions
    ]

    selected_history = []
    selected_token = None

    if token_filter:
        target = Session.query.filter_by(token=token_filter).first()
        if target:
            selected_token = token_filter
            selected_history = [
                {
                    "lat": h.lat,
                    "lng": h.lng,
                    "accuracy": h.accuracy,
                    "heading": h.heading,
                    "speed": h.speed,
                    "captured_at": (h.captured_at + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
                }
                for h in target.history.order_by(LocationHistory.captured_at.desc()).limit(MAX_HISTORY).all()
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
    app.run(debug=True, host="0.0.0.0", port=8888)
