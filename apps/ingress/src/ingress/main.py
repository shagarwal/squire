"""
Real uvicorn entrypoint: `uvicorn ingress.main:app`.

Deliberately kept separate from ingress.app -- create_app() there has no
import-time side effects and no environment requirement, which is what lets
tests import ingress.app freely without CONTROL_API_URL / INTERNAL_API_TOKEN
being set. This module is the one place that actually reads the real
process environment and constructs the module-level `app` uvicorn imports.
"""
import logging

from .app import create_app

# Plain "%(message)s" format: log_event() already emits a complete JSON
# object as its message, so we don't want logging's default prefix
# (timestamp/level/logger name) double-encoding on top of it. Railway
# captures stdout, so this is all that's needed -- no file handlers, no log
# shipping config here.
logging.basicConfig(level=logging.INFO, format="%(message)s")

app = create_app()
