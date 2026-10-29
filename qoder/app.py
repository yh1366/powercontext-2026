"""Entry point inside ``qoder/`` — same service as the repository root shim.

    cd qoder && python app.py

The implementation lives in ``qoder/backend/app.py``; this file only makes the
package directory runnable on its own, because reviewers commonly ``cd`` into
the project folder (the one holding README/requirements) and start ``app.py``.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, 'backend')

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

_spec = importlib.util.spec_from_file_location(
    'studymind_backend', os.path.join(BACKEND, 'app.py')
)
_backend = importlib.util.module_from_spec(_spec)
# Register before exec so Flask resolves the ../frontend static folder against
# the real file location instead of the current working directory.
sys.modules['studymind_backend'] = _backend
_spec.loader.exec_module(_backend)

app = _backend.app


def main():
    host = os.getenv('HOST') or os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('PORT') or os.getenv('FLASK_PORT') or '5000')
    try:
        _backend._ensure_seed_data()
    except Exception:
        pass
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
