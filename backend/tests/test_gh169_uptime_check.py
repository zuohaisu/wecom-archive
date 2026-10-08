"""GH-169 regression coverage for transport failures in external probes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/uptime-check.yml"

_PROBE_STEPS = (
    "Check public endpoint availability",
    "Check /health/ready",
)


def _probe_script(step_name: str) -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["check"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == step_name)


@pytest.mark.parametrize("step_name", _PROBE_STEPS)
@pytest.mark.parametrize(
    ("curl_exit", "http_code", "expected_success"),
    [(18, "200", False), (7, "", False), (0, "200", True), (0, "503", False)],
)
def test_endpoint_probe_checks_curl_result_and_http_status(
    tmp_path: Path,
    step_name: str,
    curl_exit: int,
    http_code: str,
    expected_success: bool,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$FAKE_HTTP_CODE"\n'
        'exit "$FAKE_CURL_EXIT"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    github_output = tmp_path / "github-output"
    github_output.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "UPTIME_BASE_URL": "https://probe.example.test",
        "GITHUB_OUTPUT": str(github_output),
        "FAKE_CURL_EXIT": str(curl_exit),
        "FAKE_HTTP_CODE": http_code,
    }

    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            _probe_script(step_name),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode == 0) is expected_success, result.stderr + result.stdout
    output = github_output.read_text(encoding="utf-8")
    assert f"http_code={http_code or '000'}" in output
    assert f"curl_exit_code={curl_exit}" in output
