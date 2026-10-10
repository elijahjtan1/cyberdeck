"""Run the backend: python3 -m core [--port 8765] [--data ~/cyberdeck-data]

Listens on 127.0.0.1 only. Install as a service with setup_core_service.py.
"""
import argparse
import logging
import signal
import sys
from pathlib import Path

from . import DEFAULT_PORT, VERSION
from .sysinfo import SystemSampler
from .telemetry import Telemetry, TelemetryStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m core', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1',
                        help='address to listen on (default 127.0.0.1: this Pi only)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--data', help='data folder (default ~/cyberdeck-data)')
    parser.add_argument('--interval', type=float, default=5., help='seconds between samples')
    args = parser.parse_args(argv)
    try:
        from werkzeug.serving import make_server
        from .server import create_app
    except ImportError:
        raise SystemExit('Flask is missing. Install it with: sudo apt install python3-flask')
    data = Path(args.data).expanduser() if args.data else Path.home() / 'cyberdeck-data'
    data.mkdir(parents=True, exist_ok=True)
    telemetry = Telemetry(TelemetryStore(data / 'telemetry.sqlite3'), SystemSampler(),
                          interval=args.interval)
    # The panel polls every few seconds: don't write a journal line per request.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    server = make_server(args.host, args.port, create_app(telemetry), threaded=True)
    # systemd stops services with SIGTERM: turn it into a clean exit so the buffer is saved.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    telemetry.start()
    print(f'cyberdeck-core {VERSION} on http://{args.host}:{args.port} (data: {data})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        telemetry.close()
        telemetry.store.close()
        print('cyberdeck-core stopped; telemetry saved.', flush=True)


if __name__ == '__main__':
    main()
