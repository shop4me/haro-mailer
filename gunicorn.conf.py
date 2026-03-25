"""Gunicorn config for production. Only touches this app’s process."""

import os

# Background scheduler in create_app() must not run in multiple workers.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8001")
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True
