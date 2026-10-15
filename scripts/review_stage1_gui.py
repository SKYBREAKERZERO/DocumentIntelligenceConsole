"""Launch local-only Stage1 review workbench; separate from Open WebUI."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.gui.stage1_review_window import main

if __name__=='__main__':
    main()
