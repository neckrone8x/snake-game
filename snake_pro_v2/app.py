import os
import re
import time
import secrets
import sqlite3
import logging
from collections import defaultdict, deque

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room

# =============================================================
# CONFIG
# =============================================================
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'snake.db')
IS_PROD = os.environ.get('FLASK_ENV', 'development') == 'production'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('snake')

app = Flask(__name__)

_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if IS_PROD:
        raise RuntimeError('SECRET_KEY must be set in production.')
    _secret = secrets.token_hex(32)
    log.warning('Using ephemeral SECRET_KEY — sessions reset on restart. '
                'Set the SECRET_KEY env var for a stable deployment.')
app.config['SECRET_KEY'] = _secret

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = IS_PROD
app.config['MAX_CONTENT_LENGTH']      = 32 * 1024  # 32 KB

allowed_origins = os.environ.get('ALLOWED_ORIGINS', '*' if not IS_PROD else '')
socketio = SocketIO(
    app,
    cors_allowed_origins=allowed_origins,
    async_mode='threading',
    ping_interval=25,
    ping_timeout=20,
)

# =============================================================
# DATABASE
# =============================================================
def db():
    con = sqlite3.connect(DB, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    try:
        con = db()
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          username      TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          created_at    INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scores (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    INTEGER,
          username   TEXT NOT NULL,
          score      INTEGER NOT NULL,
          difficulty TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scores_diff
          ON scores(difficulty, score DESC, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_scores_user
          ON scores(user_id, created_at DESC);
        ''')
        con.commit()
        con.close()
        log.info('Database ready at %s', DB)
    except Exception as e:
        log.exception('Failed to initialise database: %s', e)
        raise


# =============================================================
# PASSWORDS
# =============================================================
PBKDF2_ITER = 240_000


def pw_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PBKDF2_ITER)
    return salt.hex() + ':' + digest.hex()


def pw_ok(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(':')
        check = hashlib.pbkdf2_hmac(
            'sha256', password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITER
        ).hex()
        return secrets.compare_digest(check, digest_hex)
    except Exception:
        return False


import hashlib  # placed here so pw_hash can reference it

# =============================================================
# SIMPLE IN-MEMORY RATE LIMITER
# =============================================================
_hits = defaultdict(deque)


def rate_limit(key: str, max_calls: int, window_s: float) -> bool:
    """Return True if the call is allowed."""
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= max_calls:
        return False
    q.append(now)
    return True


def client_ip():
    fwd = request.headers.get('X-Forwarded-For')
    return (fwd.split(',')[0].strip() if fwd else request.remote_addr) or 'anon'


# =============================================================
# AUTH ENDPOINTS
# =============================================================
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,20}$')


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/me')
def me():
    return jsonify({
        'username': session.get('username'),
        'user_id': session.get('user_id'),
    })


@app.post('/api/register')
def register():
    if not rate_limit(f'reg:{client_ip()}', 5, 60):
        return jsonify(error='Too many attempts. Try again in a minute.'), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not USERNAME_RE.match(username):
        return jsonify(error='Username must be 3–20 chars: letters, numbers, underscore.'), 400
    if not 6 <= len(password) <= 128:
        return jsonify(error='Password must be at least 6 characters.'), 400

    con = db()
    try:
        cur = con.execute(
            'INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?)',
            (username, pw_hash(password), int(time.time())),
        )
        con.commit()
        session.clear()
        session['user_id'] = cur.lastrowid
        session['username'] = username
        log.info('Registered user %s', username)
        return jsonify(ok=True, username=username)
    except sqlite3.IntegrityError:
        return jsonify(error='That username is taken.'), 409
    finally:
        con.close()


@app.post('/api/login')
def login():
    if not rate_limit(f'login:{client_ip()}', 10, 60):
        return jsonify(error='Too many attempts. Try again in a minute.'), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    con = db()
    row = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    con.close()

    if not row or not pw_ok(password, row['password_hash']):
        return jsonify(error='Invalid username or password.'), 401

    session.clear()
    session['user_id'] = row['id']
    session['username'] = row['username']
    return jsonify(ok=True, username=row['username'])


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True)


# =============================================================
# SCORES
# =============================================================
VALID_DIFFS = {'easy', 'normal', 'hard', 'insane'}


@app.post('/api/scores')
def add_score():
    if not rate_limit(f'score:{client_ip()}', 30, 60):
        return jsonify(error='Slow down.'), 429

    data = request.get_json(silent=True) or {}
    try:
        score = int(data.get('score', 0))
    except (TypeError, ValueError):
        return jsonify(error='Invalid score.'), 400

    difficulty = str(data.get('difficulty', 'normal'))
    if difficulty not in VALID_DIFFS:
        return jsonify(error='Invalid difficulty.'), 400
    if not 0 <= score <= 1_000_000:
        return jsonify(error='Invalid score.'), 400

    username = session.get('username', 'Guest')
    user_id  = session.get('user_id')

    # Per-user cooldown — 1 submission every 2s
    if user_id and not rate_limit(f'score:u:{user_id}', 1, 2):
        return jsonify(error='Please wait before submitting again.'), 429

    con = db()
    con.execute(
        'INSERT INTO scores(user_id, username, score, difficulty, created_at) VALUES(?,?,?,?,?)',
        (user_id, username, score, difficulty, int(time.time())),
    )
    con.commit()
    con.close()
    return jsonify(ok=True)


@app.get('/api/leaderboard')
def leaderboard():
    difficulty = request.args.get('difficulty', 'normal')
    if difficulty not in VALID_DIFFS:
        difficulty = 'normal'

    try:
        limit = max(1, min(int(request.args.get('limit', 25)), 100))
    except ValueError:
        limit = 25

    con = db()
    rows = con.execute(
        '''SELECT username, score, difficulty, created_at
           FROM scores
           WHERE difficulty = ?
           ORDER BY score DESC, created_at ASC
           LIMIT ?''',
        (difficulty, limit),
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.get('/api/stats')
def stats():
    con = db()
    row = con.execute(
        'SELECT COUNT(*) AS games, COUNT(DISTINCT username) AS players FROM scores'
    ).fetchone()
    con.close()
    return jsonify(dict(row))


# =============================================================
# MULTIPLAYER (Socket.IO)
# =============================================================
room_state = defaultdict(dict)   # room -> { sid: state }
sid_rooms  = defaultdict(set)    # sid  -> { rooms }
state_bucket = defaultdict(deque)  # sid -> recent message timestamps

MAX_PLAYERS_PER_ROOM = 16
STATE_HZ = 30  # hard cap per client
VALID_DIRS = {'up', 'down', 'left', 'right'}


def sanitize_state(raw, sid, username):
    """Return a clean dict or None if the payload is junk."""
    if not isinstance(raw, dict):
        return None

    snake = raw.get('snake')
    if not isinstance(snake, list) or not (0 < len(snake) <= 200):
        return None

    clean_snake = []
    for seg in snake:
        if not isinstance(seg, dict):
            return None
        try:
            x = int(seg.get('x'))
            y = int(seg.get('y'))
        except (TypeError, ValueError):
            return None
        if not (0 <= x < 200 and 0 <= y < 200):
            return None
        clean_snake.append({'x': x, 'y': y})

    d = raw.get('dir')
    if d not in VALID_DIRS:
        d = 'right'

    try:
        score  = max(0, min(int(raw.get('score', 0)), 1_000_000))
    except (TypeError, ValueError):
        score = 0

    return {
        'sid': sid,
        'username': username,
        'snake': clean_snake,
        'dir': d,
        'score': score,
        'ts': int(time.time() * 1000),
    }


@socketio.on('join_game')
def on_join(data):
    data = data if isinstance(data, dict) else {}
    room = str(data.get('room', 'public'))[:32]
    room = re.sub(r'[^A-Za-z0-9_-]', '', room) or 'public'

    # Enforce room size
    if len(room_state[room]) >= MAX_PLAYERS_PER_ROOM:
        emit('room_full', {'room': room})
        return

    username = session.get('username', 'Guest')
    join_room(room)
    sid_rooms[request.sid].add(room)

    # Send the current room snapshot to the joiner
    emit('room_snapshot', {
        'room': room,
        'players': [
            s for sid, s in room_state[room].items() if sid != request.sid
        ],
    })

    # Tell the room someone arrived
    emit('player_joined',
         {'sid': request.sid, 'username': username},
         to=room, include_self=False)

    log.info('join room=%s user=%s players=%d', room, username, len(room_state[room]) + 1)


@socketio.on('player_state')
def on_state(data):
    sid = request.sid
    # Rate limit
    now = time.time()
    q = state_bucket[sid]
    while q and now - q[0] > 1.0:
        q.popleft()
    if len(q) >= STATE_HZ:
        return
    q.append(now)

    username = session.get('username', 'Guest')
    clean = sanitize_state(data, sid, username)
    if clean is None:
        return

    for room in sid_rooms.get(sid, ()):
        room_state[room][sid] = clean
        emit('player_state', clean, to=room, include_self=False)


@socketio.on('chat')
def on_chat(data):
    if not rate_limit(f'chat:{request.sid}', 5, 3):
        return
    data = data if isinstance(data, dict) else {}
    text = str(data.get('text', ''))[:140].strip()
    if not text:
        return
    username = session.get('username', 'Guest')
    for room in sid_rooms.get(request.sid, ()):
        emit('chat', {'sid': request.sid, 'username': username, 'text': text}, to=room)


@socketio.on('leave_game')
def on_leave(data):
    data = data if isinstance(data, dict) else {}
    room = str(data.get('room', 'public'))[:32]
    _drop_from_room(request.sid, room)


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    for room in list(sid_rooms.get(sid, ())):
        _drop_from_room(sid, room)


def _drop_from_room(sid, room):
    if room in sid_rooms.get(sid, ()):
        sid_rooms[sid].discard(room)
        if not sid_rooms[sid]:
            sid_rooms.pop(sid, None)

    room_state.get(room, {}).pop(sid, None)
    if room in room_state and not room_state[room]:
        room_state.pop(room, None)

    leave_room(room)
    emit('player_left', {'sid': sid}, to=room)


# =============================================================
# BOOT
# =============================================================
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    host = os.environ.get('HOST', '127.0.0.1' if IS_PROD else '0.0.0.0')
    log.info('Starting server on %s:%d (debug=%s)', host, port, debug)
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=debug)
else:
    # Production entry point (gunicorn / eventlet workers)
    init_db()