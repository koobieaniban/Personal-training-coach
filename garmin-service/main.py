"""
Garmin Connect sync service — Railway deployment
On-demand: called by the dashboard when a user marks a workout complete.

Required Railway env vars:
  SUPABASE_URL          — Supabase project URL
  SUPABASE_SERVICE_KEY  — service_role key (bypasses RLS)
  GARMIN_ENCRYPT_KEY    — Fernet key (generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  SERVICE_SECRET        — shared secret sent in X-Service-Key header by the dashboard

IMPORTANT: deploy with a single gunicorn worker (see Procfile) — MFA state is in-process.
"""
import os
import logging
import threading
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

SUPABASE_URL     = os.environ['SUPABASE_URL']
SUPABASE_KEY     = os.environ['SUPABASE_SERVICE_KEY']
SERVICE_SECRET   = os.environ.get('SERVICE_SECRET', '')
FERNET_KEY       = os.environ['GARMIN_ENCRYPT_KEY'].encode()

fernet: Fernet   = Fernet(FERNET_KEY)
sb: Client       = create_client(SUPABASE_URL, SUPABASE_KEY)

# In-process MFA sessions (single gunicorn worker only — see Procfile)
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
    for attr in ('garth',):
        obj = getattr(client, attr, None)
        if obj and hasattr(obj, 'dumps'):
            try:
                return obj.dumps()
            except Exception:
                pass
    # garth may be a module-level singleton in newer versions
    try:
        import garth as _garth
        if hasattr(_garth, 'client') and hasattr(_garth.client, 'dumps'):
            return _garth.client.dumps()
    except Exception:
        pass
    return None


def save_credentials(user_id: str, email: str, password: str, client: Optional[Garmin] = None):
    enc_password = encrypt(password)
    enc_tokens   = None
    try:
        if client:
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


def start_garmin_login(user_id: str, email: str, password: str) -> Dict[str, Any]:
    """
    Starts Garmin auth in a background thread to support MFA prompts.
    Returns one of:
      {'status': 'connected', 'client': <Garmin>}
      {'status': 'mfa_required'}
      {'status': 'error', 'error': <str>}
    """
    result: Dict[str, Any] = {
        'client':        None,
        'error':         None,
        'mfa_code':      None,
        'mfa_requested': False,
        'email':         email,
        'password':      password,
    }
    mfa_event = threading.Event()
    result['event'] = mfa_event

    def prompt_mfa() -> str:
        result['mfa_requested'] = True
        log.info('MFA required for %s — waiting for code', user_id)
        if not mfa_event.wait(timeout=300):
            raise Exception('MFA code not received within 5 minutes — please try again')
        return result.get('mfa_code') or ''

    def do_login():
        try:
            # New API (garminconnect ≥ 0.2.22): credentials + prompt_mfa in constructor
            try:
                client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
                client.login()
            except TypeError:
                # Older API: pass prompt_mfa via garth
                client = Garmin()
                client.garth.login(email, password, prompt_mfa=prompt_mfa)
            result['client'] = client
            log.info('Garmin login completed for %s', user_id)
        except Exception as e:
            result['error'] = str(e)
            log.error('Garmin login failed for %s: %s', user_id, e)

    t = threading.Thread(target=do_login, daemon=True)
    result['thread'] = t
    _pending_mfa[user_id] = result
    t.start()

    # Wait up to 25 s for login to finish OR for MFA to be triggered
    t.join(timeout=25)

    if not t.is_alive():
        del _pending_mfa[user_id]
        if result['error']:
            return {'status': 'error', 'error': result['error']}
        return {'status': 'connected', 'client': result['client']}

    if result['mfa_requested']:
        # Thread is blocking, waiting for the MFA code
        return {'status': 'mfa_required'}

    # Still running with no MFA request yet — give it more time
    t.join(timeout=20)
    if not t.is_alive():
        del _pending_mfa[user_id]
        if result['error']:
            return {'status': 'error', 'error': result['error']}
        return {'status': 'connected', 'client': result['client']}

    if result['mfa_requested']:
        return {'status': 'mfa_required'}

    # Give up
    del _pending_mfa[user_id]
    return {'status': 'error', 'error': 'Garmin login timed out (45 s)'}


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
    return jsonify({'status': 'ok'})


@app.route('/connect', methods=['POST'])
def connect():
    """
    Step 1: Verify Garmin credentials and store them (or trigger MFA).
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

    outcome = start_garmin_login(user_id, email, password)

    if outcome['status'] == 'error':
        return jsonify({'error': outcome['error']}), 400

    if outcome['status'] == 'mfa_required':
        return jsonify({'status': 'mfa_required'})

    # Connected — save encrypted credentials
    try:
        save_credentials(user_id, email, password, outcome.get('client'))
    except Exception as e:
        return jsonify({'error': f'Credentials saved but DB write failed: {e}'}), 500

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

    pending = _pending_mfa.get(user_id)
    if not pending:
        return jsonify({'error': 'No pending MFA session — please click "Connect Garmin" again first'}), 400

    # Provide code to the waiting login thread
    pending['mfa_code'] = mfa_code
    pending['event'].set()

    # Wait for login thread to finish
    t = pending.get('thread')
    if t:
        t.join(timeout=30)

    _pending_mfa.pop(user_id, None)

    if pending.get('error'):
        return jsonify({'error': pending['error']}), 400

    if not pending.get('client'):
        return jsonify({'error': 'Login did not complete after MFA — please try again'}), 500

    try:
        save_credentials(user_id, pending['email'], pending['password'], pending['client'])
    except Exception as e:
        return jsonify({'error': f'Auth succeeded but DB write failed: {e}'}), 500

    return jsonify({'status': 'connected', 'email': pending['email']})


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
        try:
            client = Garmin(email=email, password=password)
            client.login()
        except TypeError:
            client = Garmin()
            client.garth.login(email, password)
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
    try:
        sb.table('garmin_credentials').delete().eq('user_id', user_id).execute()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'status': 'disconnected'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
