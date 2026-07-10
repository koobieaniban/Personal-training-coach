"""
Garmin Connect sync service — Railway deployment
On-demand: called by the dashboard when a user marks a workout complete.

Required Railway env vars:
  SUPABASE_URL          — Supabase project URL
  SUPABASE_SERVICE_KEY  — service_role key (bypasses RLS)
  GARMIN_ENCRYPT_KEY    — Fernet key (generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  SERVICE_SECRET        — shared secret sent in X-Service-Key header by the dashboard
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
from garminconnect import Garmin
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL   = os.environ['SUPABASE_URL']
SUPABASE_KEY   = os.environ['SUPABASE_SERVICE_KEY']
SERVICE_SECRET = os.environ.get('SERVICE_SECRET', '')
FERNET_KEY     = os.environ['GARMIN_ENCRYPT_KEY'].encode()

fernet: Fernet = Fernet(FERNET_KEY)
sb: Client     = create_client(SUPABASE_URL, SUPABASE_KEY)

# In-process MFA sessions — single gunicorn worker only (see Procfile)
# Key: user_id → {'client': Garmin, 'email': str, 'password': str}
_pending_mfa: Dict[str, Dict[str, Any]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def check_auth() -> bool:
    return request.headers.get('X-Service-Key') == SERVICE_SECRET


def encrypt(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()


def dump_session(client: Garmin) -> Optional[str]:
    """Try to export garth session tokens — returns None if not available."""
    try:
        import garth as _g
        if hasattr(_g, 'client') and hasattr(_g.client, 'dumps'):
            return _g.client.dumps()
    except Exception:
        pass
    for attr in ('garth', 'client'):
        obj = getattr(client, attr, None)
        if obj and hasattr(obj, 'dumps'):
            try:
                return obj.dumps()
            except Exception:
                pass
    return None


def save_credentials(user_id: str, email: str, password: str, client: Optional[Garmin] = None):
    enc_password = encrypt(password)
    enc_tokens   = None
    if client:
        try:
            raw = dump_session(client)
            if raw:
                enc_tokens = encrypt(raw)
        except Exception:
            pass

    sb.table('garmin_credentials').upsert({
        'user_id':             user_id,
        'garmin_email':        email,
        'garmin_password_enc': enc_password,
        'garmin_tokens_enc':   enc_tokens,
        'sync_enabled':        True,
        'updated_at':          datetime.utcnow().isoformat(),
    }, on_conflict='user_id').execute()


def laps_from_splits(splits_response: dict) -> list:
    laps = []
    for lap in splits_response.get('lapDTOs', []):
        dist_m  = lap.get('distance', 0)
        dur_sec = lap.get('duration', 0)
        if dist_m < 50 or dur_sec <= 0:
            continue
        laps.append({
            'distM':        round(dist_m),
            'totalTimeSec': round(dur_sec),
            'paceSecPerKm': round((dur_sec / dist_m) * 1000) if dist_m else None,
            'avgHR':        round(lap.get('averageHR', 0)) or None,
            'maxHR':        round(lap.get('maxHR', 0)) or None,
        })
    return laps


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    try:
        import garminconnect as _gc
        gc_ver = getattr(_gc, '__version__', 'unknown')
    except Exception:
        gc_ver = 'not installed'
    try:
        import garth as _g
        g_ver = getattr(_g, '__version__', 'unknown')
    except Exception:
        g_ver = 'not installed'
    return jsonify({'status': 'ok', 'garminconnect': gc_ver, 'garth': g_ver})


@app.route('/connect', methods=['POST'])
def connect():
    """
    Step 1: Start Garmin login (may or may not require MFA).
    Body: { user_id, email, password }
    Returns: { status: 'connected' } | { status: 'mfa_required' } | { error: '...' }
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data     = request.json or {}
    user_id  = data.get('user_id', '').strip()
    email    = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not all([user_id, email, password]):
        return jsonify({'error': 'missing user_id, email, or password'}), 400

    try:
        client = Garmin(email=email, password=password, return_on_mfa=True)
        mfa_status, _ = client.login()
    except Exception as e:
        log.error('Garmin login error for %s: %s', user_id, e)
        return jsonify({'error': str(e)}), 400

    if mfa_status == 'needs_mfa':
        # Store client — MFA state is kept on the client object itself
        _pending_mfa[user_id] = {'client': client, 'email': email, 'password': password}
        log.info('MFA required for %s — waiting for code', user_id)
        return jsonify({'status': 'mfa_required'})

    # No MFA needed — login complete
    try:
        save_credentials(user_id, email, password, client)
    except Exception as e:
        return jsonify({'error': f'Connected but DB write failed: {e}'}), 500

    log.info('Garmin connected (no MFA) for %s', user_id)
    return jsonify({'status': 'connected', 'email': email})


@app.route('/connect/mfa', methods=['POST'])
def connect_mfa():
    """
    Step 2 (only when MFA required): submit the one-time code.
    Body: { user_id, code }
    Returns: { status: 'connected' } | { error: '...' }
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data     = request.json or {}
    user_id  = data.get('user_id', '').strip()
    mfa_code = data.get('code', '').strip()

    if not user_id or not mfa_code:
        return jsonify({'error': 'missing user_id or code'}), 400

    pending = _pending_mfa.pop(user_id, None)
    if not pending:
        return jsonify({'error': 'No pending MFA session — please click "Connect Garmin" again first'}), 400

    client   = pending['client']
    email    = pending['email']
    password = pending['password']

    try:
        # client_state is ignored by the library — MFA state is on the client object
        client.resume_login({}, mfa_code=mfa_code)
    except Exception as e:
        log.error('MFA resume failed for %s: %s', user_id, e)
        return jsonify({'error': str(e)}), 400

    try:
        save_credentials(user_id, email, password, client)
    except Exception as e:
        return jsonify({'error': f'Auth succeeded but DB write failed: {e}'}), 500

    log.info('Garmin MFA complete for %s', user_id)
    return jsonify({'status': 'connected', 'email': email})


@app.route('/sync', methods=['POST'])
def sync():
    """
    Fetch the latest Garmin activity for a specific session date.
    Body: { user_id, session_date }  (session_date: YYYY-MM-DD)
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data         = request.json or {}
    user_id      = data.get('user_id', '').strip()
    session_date = data.get('session_date', '').strip()

    if not all([user_id, session_date]):
        return jsonify({'error': 'missing user_id or session_date'}), 400

    try:
        res   = sb.table('garmin_credentials').select('*').eq('user_id', user_id).maybe_single().execute()
        creds = res.data
    except Exception as e:
        return jsonify({'error': f'DB error: {e}'}), 500

    if not creds or not creds.get('sync_enabled'):
        return jsonify({'error': 'No Garmin credentials — connect Garmin in your profile first'}), 404

    email    = creds['garmin_email']
    enc_pass = creds.get('garmin_password_enc')
    if not enc_pass:
        return jsonify({'error': 'Missing encrypted password — please reconnect Garmin'}), 500

    try:
        password = decrypt(enc_pass)
        client   = Garmin(email=email, password=password)
        client.login()
    except Exception as e:
        return jsonify({'error': f'Garmin authentication failed: {e}'}), 500

    try:
        sb.table('garmin_credentials').update({
            'last_synced_at': datetime.utcnow().isoformat(),
            'updated_at':     datetime.utcnow().isoformat(),
        }).eq('user_id', user_id).execute()
    except Exception:
        pass

    try:
        date       = datetime.strptime(session_date, '%Y-%m-%d')
        activities = client.get_activities_by_date(
            date.strftime('%Y-%m-%d'),
            (date + timedelta(days=1)).strftime('%Y-%m-%d'),
        )
    except Exception as e:
        return jsonify({'error': f'Failed to fetch activities: {e}'}), 500

    if not activities:
        return jsonify({'status': 'no_activity', 'date': session_date}), 200

    act         = activities[-1]
    activity_id = act.get('activityId')
    dist_m      = act.get('distance', 0) or 0
    elapsed_sec = act.get('duration', 0) or 0
    moving_sec  = act.get('movingDuration', elapsed_sec) or elapsed_sec
    avg_hr      = act.get('averageHR') or act.get('averageHeartRate') or None
    max_hr      = act.get('maxHR') or act.get('maxHeartRate') or None
    pace_sec    = round(moving_sec / (dist_m / 1000)) if dist_m and dist_m > 100 else None

    laps = []
    if activity_id:
        try:
            splits = client.get_activity_splits(activity_id)
            laps   = laps_from_splits(splits)
        except Exception as e:
            log.warning('Could not fetch splits for %s: %s', activity_id, e)

    if not laps and pace_sec and dist_m:
        laps = [{
            'distM':        round(dist_m),
            'totalTimeSec': round(moving_sec),
            'paceSecPerKm': pace_sec,
            'avgHR':        round(avg_hr) if avg_hr else None,
            'maxHR':        round(max_hr) if max_hr else None,
        }]

    act_type = act.get('activityType', {})
    workout  = {
        'source':       'garmin_connect',
        'activityId':   activity_id,
        'activityName': act.get('activityName', ''),
        'activityType': act_type.get('typeKey', '') if isinstance(act_type, dict) else str(act_type),
        'avgPaceSec':   pace_sec,
        'avgHR':        round(avg_hr) if avg_hr else None,
        'maxHR':        round(max_hr) if max_hr else None,
        'distanceM':    round(dist_m) if dist_m else None,
        'totalTimeSec': round(elapsed_sec) if elapsed_sec else None,
        'laps':         laps,
    }

    try:
        sb.table('workout_data').upsert({
            'user_id':      user_id,
            'session_date': session_date,
            'source':       'garmin_connect',
            'raw_data':     workout,
            'avg_pace_sec': pace_sec,
            'avg_hr':       round(avg_hr) if avg_hr else None,
            'distance_m':   round(dist_m) if dist_m else None,
            'laps':         laps or None,
        }, on_conflict='user_id,session_date').execute()
    except Exception as e:
        return jsonify({'error': f'DB write failed: {e}'}), 500

    return jsonify({'status': 'synced', 'workout': workout})


@app.route('/disconnect', methods=['POST'])
def disconnect():
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401
    data    = request.json or {}
    user_id = data.get('user_id', '').strip()
    if not user_id:
        return jsonify({'error': 'missing user_id'}), 400
    _pending_mfa.pop(user_id, None)
    try:
        sb.table('garmin_credentials').delete().eq('user_id', user_id).execute()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'status': 'disconnected'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
