#!/usr/bin/env python3
"""Dev entry point: python run.py → http://<host>:7170

The reloader is disabled on purpose — it would start a second process and the
APScheduler jobs would fire twice. Use gunicorn (via install.sh) in production.
"""
from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host=app.config['WEB_HOST'], port=app.config['WEB_PORT'],
            debug=app.config.get('DEBUG', False), use_reloader=False)
