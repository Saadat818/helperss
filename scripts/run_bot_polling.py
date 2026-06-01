import sys
import traceback
from pathlib import Path
import os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
BASE_DIR = Path(__file__).resolve().parents[1]
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

import helper7


print("Bot polling started")
print(f"Callback handlers: {len(helper7.bot.callback_query_handlers)}")

try:
    helper7.bot.infinity_polling(timeout=10, long_polling_timeout=5)
except Exception as exc:
    print(f"Bot polling stopped: {type(exc).__name__}: {exc}")
    traceback.print_exc()
    raise
