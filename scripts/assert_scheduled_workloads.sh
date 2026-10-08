#!/usr/bin/env bash
#
# assert_scheduled_workloads.sh — GH-104 post-deploy scheduled-workload
# assertion.
#
# Verifies the core GH-104 invariant end to end:
#
#   repo unit inventory (deploy/systemd/*.service|*.timer|*.path)
#   = deploy/systemd/WORKLOAD_MANIFEST (the one authoritative
#     classification: required / deferred / manual-oneshot / deprecated /
#     static-helper / template / out-of-scope)
#   = deploy/systemd/MANAGED_UNITS (the plain allowlist
#     scripts/deploy_server.sh actually reads)
#   = [--server mode only] installed/enabled/active state on this host
#
# Modes:
#   (default)   repo-only checks — manifest structure, ExecStart targets
#               exist, MANAGED_UNITS agrees with the manifest, destructive
#               deferred units cannot be flagged auto_install/auto_enable.
#               Safe to run anywhere (CI, a laptop) — never touches
#               systemctl.
#   --server    additionally checks REAL installed/enabled/active state
#               via $SYSTEMCTL_BIN / $SYSTEMD_UNIT_DIR. Run this on the
#               production host after a deploy.
#
# Exit codes:
#   0  every required workload is correctly represented (and, in --server
#      mode, correctly installed/enabled) — deferred/manual workloads may
#      still have produced non-fatal WARN lines.
#   1  a required workload is missing/misdeclared, a destructive deferred
#      unit is enabled, or a deprecated unit has re-entered the managed
#      set.
#
# Command/path overrides (for tests — never needed in production):
#   DEPLOY_DIR, MANIFEST, MANAGED_UNITS_FILE, SYSTEMCTL_BIN,
#   SYSTEMD_UNIT_DIR
set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MANIFEST="${MANIFEST:-$DEPLOY_DIR/deploy/systemd/WORKLOAD_MANIFEST}"
MANAGED_UNITS_FILE="${MANAGED_UNITS_FILE:-$DEPLOY_DIR/deploy/systemd/MANAGED_UNITS}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"

SERVER_MODE=0
for arg in "$@"; do
    case "$arg" in
        --server) SERVER_MODE=1 ;;
        -h|--help)
            echo "Usage: $0 [--server]"
            exit 0
            ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

FAILURES=0
_fail() {
    echo "[FAIL] $*" >&2
    FAILURES=$((FAILURES + 1))
}
_warn() {
    echo "[WARN] $*" >&2
}
_ok() {
    echo "[OK] $*"
}

if [ ! -f "$MANIFEST" ]; then
    _fail "manifest not found: $MANIFEST"
    exit 1
fi
if [ ! -f "$MANAGED_UNITS_FILE" ]; then
    _fail "MANAGED_UNITS not found: $MANAGED_UNITS_FILE"
    exit 1
fi

# ── Load MANAGED_UNITS as a plain set (comments/blank lines stripped) ─────
declare -A MANAGED_SET=()
while IFS= read -r line; do
    case "$line" in
        ''|'#'*) continue ;;
    esac
    MANAGED_SET["$line"]=1
done <"$MANAGED_UNITS_FILE"

# ── Extracts the repo-relative script/entrypoint path(s) an ExecStart=
# line references, resolved against $DEPLOY_DIR, so a required unit's
# real entrypoint can be proven to exist without hardcoding a per-job
# path list here. The deployed checkout root may vary; paths are mapped
# relative to the conventional `/current/` checkout marker. Deliberately
# ignores the venv interpreter itself
# (.venv/bin/python is created at deploy time, never committed) and any
# word that is not a .py/.sh script — a `python -m module:app` invocation
# has no on-disk script target to check. A relative ExecStart word (the
# common case — see wecom-archive-worker.service's `scripts/run_*.py`) is
# resolved against the unit's own WorkingDirectory=, not $DEPLOY_DIR
# directly.
_execstart_targets() {
    local service_file="$1" line word wd wd_rel
    line=$(grep -E '^ExecStart=' "$service_file" | head -1)
    line="${line#ExecStart=}"
    wd=$(grep -E '^WorkingDirectory=' "$service_file" | head -1)
    wd="${wd#WorkingDirectory=}"
    wd_rel=""
    if [[ "$wd" == */current/* ]]; then
        wd_rel="${wd#*/current/}"
        wd_rel="${wd_rel%/}"
    fi
    for word in $line; do
        case "$word" in
            */current/*.py | */current/*.sh)
                echo "${word#*/current/}"
                ;;
            *.py | *.sh)
                if [ -n "$wd_rel" ] && [ "$wd_rel" != "$wd" ]; then
                    echo "$wd_rel/$word"
                fi
                ;;
        esac
    done
}

echo "=== GH-104 scheduled-workload manifest checks (repo) ==="

declare -A MANIFEST_UNITS=()
LINE_NO=0
while IFS='|' read -r unit classification repo_status auto_install auto_enable trigger_type destructive notes; do
    LINE_NO=$((LINE_NO + 1))
    case "$unit" in
        ''|'#'*) continue ;;
    esac
    if [ -z "${notes+x}" ]; then
        _fail "manifest line $LINE_NO ($unit): expected 8 pipe-delimited fields"
        continue
    fi
    MANIFEST_UNITS["$unit"]=1

    is_trigger=0
    case "$unit" in
        *.timer)
            is_trigger=1
            if [ "$trigger_type" != "template" ] && [ "$trigger_type" != "timer" ]; then
                _fail "$unit: a .timer unit's trigger_type should be 'timer' (or 'template' for an uninstantiated @ template), got '$trigger_type'"
            fi
            ;;
        *.path)
            is_trigger=1
            [ "$trigger_type" = "path" ] || _fail "$unit: a .path unit's trigger_type should be 'path', got '$trigger_type'"
            ;;
    esac

    # repo_status invariants
    case "$repo_status" in
        present)
            src="$DEPLOY_DIR/deploy/systemd/$unit"
            if [ ! -f "$src" ]; then
                _fail "$unit: manifest says repo_status=present but $src does not exist"
                continue
            fi
            # Only required/static-helper units are expected to have a
            # checkable on-disk script entrypoint under
            # /srv/apps/wecom-archive-365/current/ — an out-of-scope unit
            # like the nonprod web service lives under a different path
            # prefix and/or invokes a module rather than a script.
            if [[ "$unit" == *.service ]] && { [ "$classification" = "required" ] || [ "$classification" = "static-helper" ]; }; then
                targets=$(_execstart_targets "$src")
                execstart_line=$(grep -E '^ExecStart=' "$src" | head -1)
                if [ -z "$targets" ]; then
                    # A `python -m some.module` invocation (e.g. the daily
                    # external-contact reconcile) legitimately has no
                    # on-disk script path to check.
                    case "$execstart_line" in
                        *' -m '*) ;;
                        *) _fail "$unit: could not find a checkable ExecStart= script target under /srv/apps/wecom-archive-365/current/" ;;
                    esac
                else
                    while IFS= read -r rel; do
                        [ -z "$rel" ] && continue
                        if [ ! -f "$DEPLOY_DIR/$rel" ]; then
                            _fail "$unit: ExecStart references missing entrypoint $rel"
                        fi
                    done <<<"$targets"
                fi
            fi
            ;;
        absent)
            src="$DEPLOY_DIR/deploy/systemd/$unit"
            if [ -f "$src" ]; then
                _fail "$unit: manifest says repo_status=absent but $src exists — update the manifest to present"
            fi
            if [ "$auto_install" != "false" ] || [ "$auto_enable" != "false" ]; then
                _fail "$unit: repo_status=absent must have auto_install=false and auto_enable=false (nothing to install)"
            fi
            ;;
        *)
            _fail "$unit: unrecognized repo_status '$repo_status' (expected present|absent)"
            ;;
    esac

    # classification / auto_install / auto_enable self-consistency
    case "$classification" in
        required|static-helper) ;;
        deferred|manual-oneshot|deprecated|template|out-of-scope)
            if [ "$auto_install" != "false" ] || [ "$auto_enable" != "false" ]; then
                _fail "$unit: classification=$classification must never have auto_install/auto_enable=true"
            fi
            ;;
        *)
            _fail "$unit: unrecognized classification '$classification'"
            ;;
    esac

    if [ "$destructive" = "true" ] && { [ "$auto_install" = "true" ] || [ "$auto_enable" = "true" ]; }; then
        _fail "$unit: destructive=true units must never be auto_install/auto_enable=true"
    fi

    if [ "$is_trigger" -eq 1 ] && [ "$auto_install" = "true" ] && [ "$auto_enable" != "true" ]; then
        _fail "$unit: a .timer/.path with auto_install=true must also have auto_enable=true"
    fi
    if [ "$is_trigger" -eq 0 ] && [ "$auto_enable" = "true" ]; then
        _fail "$unit: only a .timer/.path may have auto_enable=true (a .service is never enabled directly)"
    fi

    # MANAGED_UNITS agreement
    is_managed=0
    [ -n "${MANAGED_SET[$unit]:-}" ] && is_managed=1
    if [ "$auto_install" = "true" ] && [ "$is_managed" -eq 0 ]; then
        _fail "$unit: manifest says auto_install=true but is absent from MANAGED_UNITS"
    fi
    if [ "$auto_install" = "false" ] && [ "$is_managed" -eq 1 ]; then
        _fail "$unit: manifest says auto_install=false (classification=$classification) but is listed in MANAGED_UNITS — this unit could be silently auto-installed/enabled"
    fi
done <"$MANIFEST"

# Every MANAGED_UNITS entry must be declared in the manifest — otherwise
# someone could add a unit to the allowlist without ever classifying it.
while IFS= read -r line; do
    case "$line" in
        ''|'#'*) continue ;;
    esac
    if [ -z "${MANIFEST_UNITS[$line]:-}" ]; then
        _fail "MANAGED_UNITS lists $line but it has no WORKLOAD_MANIFEST row"
    fi
done <"$MANAGED_UNITS_FILE"

if [ "$FAILURES" -eq 0 ]; then
    _ok "manifest / MANAGED_UNITS / repo entrypoints are consistent"
fi

if [ "$SERVER_MODE" -eq 1 ]; then
    echo "=== GH-104 scheduled-workload runtime checks (server) ==="
    # auto_install/auto_enable/trigger_type/notes are already fully
    # validated in repo mode above; this loop only needs
    # classification/destructive plus the unit's own filename suffix.
    # shellcheck disable=SC2034
    while IFS='|' read -r unit classification repo_status auto_install auto_enable trigger_type destructive notes; do
        case "$unit" in
            ''|'#'*) continue ;;
        esac
        [ "$repo_status" = "absent" ] && continue

        unit_path="$SYSTEMD_UNIT_DIR/$unit"
        is_trigger=0
        case "$unit" in
            *.timer|*.path) is_trigger=1 ;;
        esac

        case "$classification" in
            required)
                if [ ! -f "$unit_path" ]; then
                    _fail "$unit: required but not installed at $unit_path"
                    continue
                fi
                if [ "$is_trigger" -eq 1 ]; then
                    state=$("$SYSTEMCTL_BIN" is-enabled "$unit" 2>/dev/null || true)
                    if [ "$state" != "enabled" ]; then
                        _fail "$unit: required trigger is not enabled (is-enabled reported '$state')"
                    fi
                    active=$("$SYSTEMCTL_BIN" is-active "$unit" 2>/dev/null || true)
                    if [ "$active" != "active" ]; then
                        _fail "$unit: required trigger is not active (is-active reported '$active')"
                    fi
                fi
                ;;
            static-helper)
                if [ ! -f "$unit_path" ]; then
                    _fail "$unit: static-helper entrypoint not installed at $unit_path"
                fi
                # Deliberately no enabled/active check — a static-helper
                # unit is only ever instantiated on demand.
                ;;
            deferred|manual-oneshot|deprecated)
                [ "$is_trigger" -eq 0 ] && continue
                if [ ! -f "$unit_path" ]; then
                    continue
                fi
                state=$("$SYSTEMCTL_BIN" is-enabled "$unit" 2>/dev/null || true)
                if [ "$state" = "enabled" ]; then
                    if [ "$destructive" = "true" ] || [ "$classification" = "deprecated" ]; then
                        _fail "$unit: classification=$classification but is ENABLED in production — must be disabled"
                    else
                        _warn "$unit: classification=$classification but is enabled in production — confirm this was an intentional, approved activation"
                    fi
                fi
                ;;
            template|out-of-scope)
                continue
                ;;
        esac
    done <"$MANIFEST"
fi

if [ "$FAILURES" -gt 0 ]; then
    echo "=== $FAILURES check(s) failed ===" >&2
    exit 1
fi
echo "=== all checks passed ==="
exit 0
