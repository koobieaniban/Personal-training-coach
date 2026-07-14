"""
Garmin Connect sync service — Railway deployment
On-demand: called by the dashboard when a user marks a workout complete.

Required Railway env vars:
  SUPABASE_URL          — Supabase project URL
  SUPABASE_SERVICE_KEY  — service_role key (bypasses RLS)
  GARMIN_ENCRYPT_KEY    — Fernet key
  SERVICE_SECRET        — shared secret sent in X-Service-Key header
  ANTHROPIC_API_KEY     — for screenshot analysis (/analyze-screenshot endpoint)

IMPORTANT: deploy with a single gunicorn worker (see Procfile).
MFA state is held in-process; multiple workers would lose it.
"""
import os
import re
import json
import time
import logging
import tempfile
import threading
import importlib.metadata
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
_pending_mfa: Dict[str, Dict[str, Any]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def check_auth() -> bool:
    return request.headers.get('X-Service-Key') == SERVICE_SECRET


def encrypt(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()


def pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return 'unknown'


def save_credentials(user_id: str, email: str, password: str):
    sb.table('garmin_credentials').upsert({
        'user_id':             user_id,
        'garmin_email':        email,
        'garmin_password_enc': encrypt(password),
        'sync_enabled':        True,
        'updated_at':          datetime.utcnow().isoformat(),
    }, on_conflict='user_id').execute()


def save_garth_tokens(client: Garmin, user_id: str):
    """Persist garth OAuth tokens to Supabase after a successful login."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            client.garth.dump(tmpdir)
            files = {}
            for fname in os.listdir(tmpdir):
                fpath = os.path.join(tmpdir, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        files[fname] = f.read()
            if not files:
                log.warning('No garth token files found for %s', user_id)
                return
            sb.table('garmin_credentials').update({
                'garmin_tokens_enc': encrypt(json.dumps(files)),
                'updated_at':        datetime.utcnow().isoformat(),
            }).eq('user_id', user_id).execute()
            log.info('Saved garth tokens (%d files) for %s', len(files), user_id)
    except Exception as e:
        log.warning('save_garth_tokens failed for %s: %s', user_id, e)


def load_garth_client(creds: dict) -> Optional[Garmin]:
    """
    Try to restore a Garmin client from stored OAuth tokens.
    garth auto-refreshes an expired access_token using the refresh_token,
    so this works for hours-to-days without requiring a new full login.
    Returns None if tokens are missing, invalid, or unrestorable.
    """
    import concurrent.futures
    import garth as garth_lib

    enc = creds.get('garmin_tokens_enc')
    if not enc:
        return None
    try:
        files = json.loads(decrypt(enc))
        with tempfile.TemporaryDirectory() as tmpdir:
            for fname, content in files.items():
                with open(os.path.join(tmpdir, fname), 'w') as f:
                    f.write(content)
            # garth.Client.load() is a classmethod — it returns a NEW Client
            # with tokens loaded.  Calling it as an instance method discards
            # the return value and leaves the client unauthenticated, which
            # was the previous bug.
            loaded_garth = garth_lib.Client.load(tmpdir)
            client = Garmin()
            client.garth = loaded_garth   # replace the empty client
            # Validate tokens with a 30s timeout — stale/invalid tokens can cause
            # garth to hang indefinitely on the OAuth refresh, which previously
            # caused gunicorn to kill the worker and return an HTML 500 page.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(client.get_user_profile)
                try:
                    fut.result(timeout=30)
                except concurrent.futures.TimeoutError:
                    raise Exception('Garmin API timed out during token validation')
            log.info('Restored Garmin session from stored tokens')
            return client
    except Exception as e:
        log.info('Token restore failed: %s', e)
        return None


def start_garmin_login(user_id: str, email: str, password: str) -> Dict[str, Any]:
    """
    Start Garmin login in a background thread so we can handle MFA.

    The thread blocks inside prompt_mfa() waiting for an MFA code.
    mfa_needed fires the instant Garmin requests a code, so the HTTP
    response returns quickly and the user sees the prompt right away.

    Returns one of:
      {'status': 'connected', 'client': <Garmin>}
      {'status': 'mfa_required'}
      {'status': 'error', 'error': str}
    """
    result: Dict[str, Any] = {
        'client':        None,
        'error':         None,
        'mfa_code':      None,
        'mfa_requested': False,
        'email':         email,
        'password':      password,
    }
    mfa_code_event = threading.Event()   # unblocks thread when user submits code
    mfa_needed     = threading.Event()   # fires the instant MFA is requested

    def prompt_mfa(*args) -> str:
        result['mfa_requested'] = True
        mfa_needed.set()                 # wake main thread immediately
        log.info('MFA required for %s — waiting for code', user_id)
        if not mfa_code_event.wait(timeout=300):
            raise Exception('MFA code not received within 5 minutes — please try again')
        return result.get('mfa_code') or ''

    def do_login():
        try:
            client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
            client.login()
            result['client'] = client
            log.info('Garmin login complete for %s', user_id)
        except Exception as e:
            result['error'] = str(e)
            log.error('Garmin login failed for %s: %s', user_id, e)
        finally:
            mfa_needed.set()  # always wake main thread

    t = threading.Thread(target=do_login, daemon=True)
    result['thread']    = t
    result['code_event'] = mfa_code_event
    _pending_mfa[user_id] = result
    t.start()

    # Wait up to 90 s for login to finish OR for Garmin to request an MFA code
    mfa_needed.wait(timeout=90)

    if not t.is_alive():
        _pending_mfa.pop(user_id, None)
        if result['error']:
            return {'status': 'error', 'error': result['error']}
        return {'status': 'connected', 'client': result['client']}

    if result['mfa_requested']:
        log.info('Returning mfa_required for %s (thread still waiting for code)', user_id)
        return {'status': 'mfa_required'}

    # Thread is still alive but hasn't requested MFA yet — Garmin is slow.
    # Keep the pending session so that if MFA arrives late the user can still
    # submit the code via /connect/mfa.  Do NOT pop from _pending_mfa here.
    log.info('Login timed out for %s but keeping session alive for late MFA', user_id)
    return {'status': 'mfa_pending'}


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
    return jsonify({
        'status':         'ok',
        'garminconnect':  pkg_version('garminconnect'),
        'garth':          pkg_version('garth'),
        'python':         __import__('sys').version,
    })


@app.route('/connect', methods=['POST'])
def connect():
    """
    Step 1: Start Garmin login. May or may not require MFA.
    Body: { user_id, email, password }
    Returns: { status: 'connected' } | { status: 'mfa_required' } | { error }
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

    if outcome['status'] in ('mfa_required', 'mfa_pending'):
        # mfa_pending = Garmin is slow; session kept alive so user can still submit a code
        return jsonify({'status': 'mfa_required'})

    try:
        save_credentials(user_id, email, password)
    except Exception as e:
        return jsonify({'error': f'Connected but credential save failed: {e}'}), 500

    save_garth_tokens(outcome['client'], user_id)  # best-effort; logs on failure

    log.info('Garmin connected (no MFA) for %s', user_id)
    return jsonify({'status': 'connected', 'email': email})


@app.route('/connect/mfa', methods=['POST'])
def connect_mfa():
    """
    Step 2 (only when MFA required): submit the one-time code.
    The background thread is still running, blocked waiting for this code.
    Body: { user_id, code }
    Returns: { status: 'connected' } | { error }
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

    # Unblock the waiting thread with the code
    pending['mfa_code'] = mfa_code
    pending['code_event'].set()

    # Wait for thread to finish (login completes after code is submitted)
    t = pending.get('thread')
    if t:
        t.join(timeout=30)

    _pending_mfa.pop(user_id, None)

    if pending.get('error'):
        return jsonify({'error': pending['error']}), 400

    if not pending.get('client'):
        return jsonify({'error': 'Login did not complete after MFA — please try again'}), 500

    email    = pending['email']
    password = pending['password']

    try:
        save_credentials(user_id, email, password)
    except Exception as e:
        log.error('Credential save failed for %s: %s', user_id, e)
        return jsonify({'error': f'Auth succeeded but credential save failed: {e}'}), 500

    save_garth_tokens(pending['client'], user_id)  # best-effort; logs on failure

    log.info('Garmin MFA complete for %s', user_id)
    return jsonify({'status': 'connected', 'email': email})


@app.route('/sync', methods=['POST'])
def sync():
    """
    Fetch the latest Garmin activity for a specific session date.
    Tries stored OAuth tokens first (no re-login needed).
    Falls back to credential login only if tokens are missing or invalid.
    Body: { user_id, session_date }  (YYYY-MM-DD)
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

    # Try token-based restore first (avoids full re-login / MFA prompt)
    client = load_garth_client(creds)

    if not client:
        # No valid tokens — credential-only login will fail if Garmin requires MFA.
        # Tell the user to reconnect so the full MFA flow runs and tokens get saved.
        return jsonify({
            'error': 'Garmin session expired — please reconnect Garmin in your profile settings to refresh your session.'
        }), 401

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


@app.route('/analyze-screenshot', methods=['POST'])
def analyze_screenshot():
    """
    Analyze a Garmin activity screenshot using Claude vision (server-side).
    Users never need their own API key — ANTHROPIC_API_KEY is a Railway env var.
    Body: { image_base64: str, media_type: str }
    Returns: { status: 'ok', data: { total_distance_km, avg_pace_sec_per_km, ... } }
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data       = request.json or {}
    b64_image  = data.get('image_base64', '').strip()
    media_type = data.get('media_type', 'image/jpeg').strip()

    if not b64_image:
        return jsonify({'error': 'missing image_base64'}), 400

    anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not anthropic_key:
        return jsonify({'error': 'Screenshot analysis not configured on server (ANTHROPIC_API_KEY missing)'}), 503

    prompt = (
        'This is a Garmin fitness activity screenshot. Extract every visible workout metric '
        'and return ONLY a JSON object — no prose, no markdown fences.\n\n'
        'Required format:\n'
        '{\n'
        '  "total_distance_km": <number or null>,\n'
        '  "total_time_seconds": <number or null>,\n'
        '  "avg_pace_sec_per_km": <number or null>,\n'
        '  "avg_hr_bpm": <number or null>,\n'
        '  "max_hr_bpm": <number or null>,\n'
        '  "calories": <number or null>,\n'
        '  "laps": [\n'
        '    { "km": <lap number>, "pace_sec_per_km": <number or null>, "avg_hr": <number or null> }\n'
        '  ]\n'
        '}\n\n'
        'Conversion rules:\n'
        '- Pace MM:SS/km → total seconds (5:30 = 330, 6:15 = 375). Valid running pace is '
        '180–900 sec/km (3:00–15:00/km). If a value labelled as pace exceeds 900 sec/km it '
        'is almost certainly the total activity time — put it in total_time_seconds instead '
        'and compute avg_pace_sec_per_km = total_time_seconds / total_distance_km.\n'
        '- Time H:MM:SS or MM:SS → total seconds (28:45 = 1725, 1:02:30 = 3750)\n'
        '- If a per-km splits table is visible, populate the laps array in order\n'
        '- If no splits table is visible, return laps as []\n'
        '- Use null for any value not visible in the screenshot'
    )

    try:
        import anthropic as ant
        ant_client = ant.Anthropic(api_key=anthropic_key)
        response = ant_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': media_type,
                            'data': b64_image,
                        },
                    },
                    {'type': 'text', 'text': prompt},
                ],
            }],
        )
        text  = response.content[0].text
        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            return jsonify({'error': 'No JSON in Claude response — try a clearer screenshot'}), 500
        result = json.loads(match.group())
        return jsonify({'status': 'ok', 'data': result})
    except Exception as e:
        log.error('Screenshot analysis failed: %s', e)
        return jsonify({'error': str(e)}), 500


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
