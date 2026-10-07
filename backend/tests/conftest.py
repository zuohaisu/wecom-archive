"""Keep the legacy regression suite explicit about its hosted baseline."""

from __future__ import annotations

import os

# Existing end-to-end tests pin hosted routes and subscription behavior. Run
# them in cloud mode by default; GH-168's selfhost tests opt in explicitly.
os.environ["APP_EDITION"] = "cloud"
