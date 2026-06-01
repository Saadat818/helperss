import os
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).resolve().parents[1]
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

from wsgi import app


print("Flask dev server starting on http://127.0.0.1:5003")
app.run(host='127.0.0.1', port=5003, debug=False, use_reloader=False)
