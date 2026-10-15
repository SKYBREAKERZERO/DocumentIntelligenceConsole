"""Per-user GUI autostart entry. Do not run the data services in this process."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
os.chdir(project_root)
sys.path.insert(0, str(project_root))

try:
    from app.gui.application import main
    main()
except BaseException:
    logdir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DocumentIntelligenceConsole" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    with (logdir / "gui_autostart_error.log").open("a", encoding="utf-8") as file:
        file.write(datetime.now().isoformat() + "\n")
        traceback.print_exc(file=file)
    raise
