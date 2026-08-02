"""CLI safety contract for RND-339 automation."""
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_reachability_automation_once.py"


def test_cli_has_only_mode_argument_and_uses_shared_lock() -> None:
    source = SCRIPT.read_text()
    assert 'add_argument("mode", choices=sorted(AUTOMATION_MODES))' in source
    assert "tenant_id" not in source.split("def _parse_args", 1)[1].split("def _acquire_lock", 1)[0]
    assert "REACHABILITY_AUTOMATION_LOCK_PATH" in source
    assert "traceback" not in source
