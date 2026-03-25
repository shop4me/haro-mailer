"""Gunicorn config for production. Only touches this app’s process."""

import os

# Background scheduler in create_app() must not run in multiple workers.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
# Dedicated port — 8001 is used by another app on the shared server.
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:18080")
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True
