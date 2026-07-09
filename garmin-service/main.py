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
import json
import logging
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
from garminconnect import Garmin, GarminConnectAuthenticationError
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # allow cross-origin requests from the dashboard

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL     = os.environ['SUPABASE_URL']
SUPABASE_KEY     = os.environ['SUPABASE_SERVICE_KEY']
SERVICE_SECRET   = os.environ.get('SERVICE_SECRET', '')
FERNET_KEY       = os.environ['GARMIN_ENCRYPT_KEY'].encode()

fernet: Fernet = Fernet(FERNET_KEY)
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def check_auth() -> bool:
    return request.headers.get('X-Service-Key') == SERVICE_SECRET


def encrypt(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()


def garmin_login(email: str, password: str) -> Garmin:
    """Auth with email+password; raises on failure."""
    client = Garmin(email, password)
    client.login()
    return client


def laps_from_splits(splits_response: dict) -> list:
    """Extract normalised lap list from get_activity_splits() response."""
    laps = []
    for lap in splits_response.get('lapDTOs', []):
        dist_m = lap.get('distance', 0)
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
    Verify Garmin credentials and store them encrypted.
    Body: { user_id, email, password }
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data = request.json or {}
    user_id  = data.get('user_id', '').strip()
    email    = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not all([user_id, email, password]):
        return jsonify({'error': 'missing user_id, email, or password'}), 400

    # Verify by actually logging in
    try:
        garmin_login(email, password)
    except GarminConnectAuthenticationError as e:
        return jsonify({'error': f'Invalid Garmin credentials: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': f'Garmin login failed: {str(e)}'}), 400

    # Store encrypted password (we re-auth each sync — simple and reliable)
    try:
        sb.table('garmin_credentials').upsert({
            'user_id':            user_id,
            'garmin_email':       email,
            'garmin_password_enc': encrypt(password),
            'sync_enabled':       True,
            'updated_at':         datetime.utcnow().isoformat(),
        }, on_conflict='user_id').execute()
    except Exception as e:
        log.error('Failed to save garmin credentials: %s', e)
        return jsonify({'error': 'Failed to save credentials'}), 500

    return jsonify({'status': 'connected', 'email': email})


@app.route('/sync', methods=['POST'])
def sync():
    """
    Fetch the latest Garmin activity for a specific session date and write
    the result to workout_data in Supabase.
    Body: { user_id, session_date }   (session_date: YYYY-MM-DD)
    """
    if not check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    data        = request.json or {}
    user_id     = data.get('user_id', '').strip()
    session_date = data.get('session_date', '').strip()

    if not all([user_id, session_date]):
        return jsonify({'error': 'missing user_id or session_date'}), 400

    # Load stored credentials
    try:
        res = sb.table('garmin_credentials').select('*').eq('user_id', user_id).maybe_single().execute()
        creds = res.data
    except Exception as e:
        return jsonify({'error': f'DB error: {str(e)}'}), 500

    if not creds or not creds.get('sync_enabled'):
        return jsonify({'error': 'no garmin credentials found — connect Garmin first'}), 404

    email    = creds['garmin_email']
    enc_pass = creds.get('garmin_password_enc')
    if not enc_pass:
        return jsonify({'error': 'missing encrypted password'}), 500

    # Authenticate
    try:
        password = decrypt(enc_pass)
        client = garmin_login(email, password)
    except Exception as e:
        return jsonify({'error': f'Garmin authentication failed: {str(e)}'}), 500

    # Update last_synced_at
    try:
        sb.table('garmin_credentials').update({
            'last_synced_at': datetime.utcnow().isoformat(),
            'updated_at':     datetime.utcnow().isoformat(),
        }).eq('user_id', user_id).execute()
    except Exception:
        pass

    # Fetch activities for the session date
    try:
        date     = datetime.strptime(session_date, '%Y-%m-%d')
        end_date = date + timedelta(days=1)
        activities = client.get_activities_by_date(
            date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d'),
        )
    except Exception as e:
        return jsonify({'error': f'Failed to fetch activities: {str(e)}'}), 500

    if not activities:
        return jsonify({'status': 'no_activity', 'date': session_date}), 200

    # Use last activity of that day
    act         = activities[-1]
    activity_id = act.get('activityId')
    dist_m      = act.get('distance', 0) or 0
    elapsed_sec = act.get('duration', 0) or 0
    moving_sec  = act.get('movingDuration', elapsed_sec) or elapsed_sec
    avg_hr      = act.get('averageHR') or act.get('averageHeartRate') or None
    max_hr      = act.get('maxHR') or act.get('maxHeartRate') or None

    pace_sec = round(moving_sec / (dist_m / 1000)) if dist_m and dist_m > 100 else None

    # Attempt to get lap splits
    laps = []
    if activity_id:
        try:
            splits = client.get_activity_splits(activity_id)
            laps = laps_from_splits(splits)
        except Exception as e:
            log.warning('Could not fetch splits for activity %s: %s', activity_id, e)

    # If no laps returned, synthesize one covering the full activity
    if not laps and pace_sec and dist_m:
        laps = [{
            'distM':        round(dist_m),
            'totalTimeSec': round(moving_sec),
            'paceSecPerKm': pace_sec,
            'avgHR':        round(avg_hr) if avg_hr else None,
            'maxHR':        round(max_hr) if max_hr else None,
        }]

    act_type = act.get('activityType', {})
    act_type_key = act_type.get('typeKey', '') if isinstance(act_type, dict) else str(act_type)

    workout = {
        'source':       'garmin_connect',
        'activityId':   activity_id,
        'activityName': act.get('activityName', ''),
        'activityType': act_type_key,
        'avgPaceSec':   pace_sec,
        'avgHR':        round(avg_hr) if avg_hr else None,
        'maxHR':        round(max_hr) if max_hr else None,
        'distanceM':    round(dist_m) if dist_m else None,
        'totalTimeSec': round(elapsed_sec) if elapsed_sec else None,
        'laps':         laps,
    }

    # Write to Supabase workout_data
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
        log.error('Failed to write workout_data: %s', e)
        return jsonify({'error': f'DB write failed: {str(e)}'}), 500

    return jsonify({'status': 'synced', 'workout': workout})


@app.route('/disconnect', methods=['POST'])
def disconnect():
    """Remove stored Garmin credentials for a user."""
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
