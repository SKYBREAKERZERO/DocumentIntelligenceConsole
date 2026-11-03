"""Launch the isolated local PostgreSQL Stage 2 reviewer GUI."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gui.stage2_review_window import main


if __name__ == "__main__":
    main()
