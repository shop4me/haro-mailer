#!/usr/bin/env python3
"""
Enable pipeline auto-send for Regency Shop (and global flags required for SMTP send).

Run from project root:
  .venv/bin/python scripts/enable_regency_auto_send.py

Requires: Regency business row + linked mailbox with SMTP configured in the UI.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sqlalchemy import select

from app.db import get_session, init_db
from app.models import AppSetting, Business
from app.regency_niche_gate import is_regency_business


def main() -> int:
    init_db()
    with get_session() as session:
        for key, val in (
            ("GLOBAL_AUTO_SEND", "true"),
            ("GLOBAL_DRY_RUN", "false"),
            ("GLOBAL_REVIEW_MODE", "false"),
        ):
            row = session.scalar(select(AppSetting).where(AppSetting.key == key))
            if row:
                row.value = val
            else:
                session.add(AppSetting(key=key, value=val))
            print("%s=%s" % (key, val), flush=True)

        businesses = session.scalars(select(Business).where(Business.enabled.is_(True))).all()
        regency = next((b for b in businesses if is_regency_business(b)), None)
        if not regency:
            print("No enabled business named like Regency Shop; enable Auto-send on that business in /businesses.", flush=True)
            return 1
        regency.auto_send_enabled = True
        if regency.auto_send_threshold > 0.85:
            regency.auto_send_threshold = 0.8
        print(
            "Regency business id=%s: auto_send_enabled=True, auto_send_threshold=%s"
            % (regency.id, regency.auto_send_threshold),
            flush=True,
        )
    print("Done. Open Settings to confirm; link Regency to a mailbox with SMTP if not already.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
