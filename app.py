"""Entry point for running StudyMind from the repository root.

The project lives in ``qoder/`` (that layout is required by the competition).
This thin shim only puts ``qoder/backend`` on ``sys.path`` and re-exports its
Flask app, so both of these work and start the identical service:

    python app.py                 # from the repository root
    python qoder/backend/app.py   # from inside qoder/

It deliberately contains no business logic - the implementation stays in one
place under ``qoder/``.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, 'qoder', 'backend')

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

_spec = importlib.util.spec_from_file_location(
    'studymind_backend', os.path.join(BACKEND, 'app.py')
)
_backend = importlib.util.module_from_spec(_spec)
# Register before exec: Flask derives its root_path (and therefore the
# ../frontend static folder) from sys.modules[__name__].__file__. Without this
# the static folder resolves against the CWD and every page route 404s.
sys.modules['studymind_backend'] = _backend
_spec.loader.exec_module(_backend)

app = _backend.app


if __name__ == '__main__':
    host = os.getenv('HOST') or os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('PORT') or os.getenv('FLASK_PORT') or '5000')
    try:
        _backend._ensure_seed_data()
    except Exception:
        pass
    app.run(host=host, port=port, debug=False)
