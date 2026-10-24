"""HTTP API on localhost. JSON in and out; see docs/CORE.md for the endpoints."""
import time

from flask import Flask, jsonify, request

from . import VERSION
from .sysinfo import decode_flags, health_alerts
from .telemetry import ACTIVITY_KINDS
from .gps import Gps
from .hardware.ld2450 import Radar
from .librarian.api import create_blueprint

HISTORY_LIMIT = 7 * 86400


def create_app(telemetry, started=None, gps=None, radar=None):
    app = Flask('cyberdeck-core')
    gps = gps or Gps()
    radar = radar or Radar()
    app.register_blueprint(create_blueprint(telemetry=telemetry))
    started = time.time() if started is None else started

    def bad(message):
        return jsonify({'error': message}), 400

    @app.get('/api/health')
    def health():
        return jsonify({'ok': True, 'service': 'cyberdeck-core', 'version': VERSION,
                        'uptime_s': round(time.time() - started)})

    @app.get('/api/telemetry/now')
    def now():
        state = telemetry.now()
        sample = state['sample'] or {}
        rows = {**decode_flags(sample.get('throttled')), **sample}
        state['alerts'] = [list(a) for a in health_alerts(rows)]
        state['busy'] = sorted({a['kind'] for a in state['active']})
        return jsonify(state)

    @app.get('/api/telemetry/history')
    def history():
        try:
            seconds = float(request.args.get('seconds', 3600))
            points = int(request.args.get('points', 240))
        except ValueError:
            return bad('seconds and points must be numbers')
        if not 60 <= seconds <= HISTORY_LIMIT or not 10 <= points <= 1000:
            return bad('seconds must be 60-604800 and points 10-1000')
        return jsonify(telemetry.history(seconds, points))

    @app.get('/api/gps')
    def gps_now():
        return jsonify(gps.snapshot())

    @app.get('/api/radar')
    def radar_now():
        return jsonify(radar.snapshot())

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.post('/api/activity/start')
    def activity_start():
        data = body()
        if data.get('kind') not in ACTIVITY_KINDS:
            return bad(f"kind must be one of {', '.join(ACTIVITY_KINDS)}")
        return jsonify({'id': telemetry.begin(data['kind'], str(data.get('detail', '')))})

    @app.post('/api/activity/end')
    def activity_end():
        ident = body().get('id')
        if type(ident) is not int:
            return bad('id must be the number returned by /api/activity/start')
        return jsonify({'ended': telemetry.end(ident)})

    @app.post('/api/activity/mark')
    def activity_mark():
        data = body()
        if data.get('kind') not in ACTIVITY_KINDS:
            return bad(f"kind must be one of {', '.join(ACTIVITY_KINDS)}")
        telemetry.mark(data['kind'], str(data.get('detail', '')))
        return jsonify({'ok': True})

    return app
