#!/bin/sh
set -u
TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REWARD_FILE=${REWARD_FILE:-/logs/verifier/reward.txt}
STATUS_FILE=${VERIFICATION_STATUS_FILE:-$(dirname "$REWARD_FILE")/verification-status.txt}
mkdir -p "$(dirname "$REWARD_FILE")" "$(dirname "$STATUS_FILE")"

write_reward() {
    chmod 0644 "$REWARD_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$REWARD_FILE"
    chmod 0444 "$REWARD_FILE" 2>/dev/null || true
}

write_status() {
    chmod 0644 "$STATUS_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$STATUS_FILE"
    chmod 0444 "$STATUS_FILE" 2>/dev/null || true
}

TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
on_interrupt() {
    write_reward 0.000000
    write_status "INCOMPLETE: verifier interrupted before completion"
    cleanup
    exit 1
}
trap cleanup EXIT
trap on_interrupt INT TERM HUP

write_reward 0.000000
write_status "INCOMPLETE: verification has not completed"

# GNU timeout is present in the Linux verifier image. The fallback keeps the
# same literal entrypoint usable on author workstations without GNU coreutils.
if ! command -v timeout >/dev/null 2>&1; then
    timeout() {
        _timeout_limit=
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --signal=*) shift ;;
                *s|*m|*h) _timeout_limit=$1; shift; break ;;
                *) break ;;
            esac
        done
        case "$_timeout_limit" in
            *s) _timeout_limit=${_timeout_limit%s} ;;
            *m) _timeout_limit=$(( ${_timeout_limit%m} * 60 )) ;;
            *h) _timeout_limit=$(( ${_timeout_limit%h} * 3600 )) ;;
        esac
        python3 -c 'import signal, subprocess, sys
limit = float(sys.argv[1])
proc = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)
try:
    code = proc.wait(timeout=limit)
except subprocess.TimeoutExpired:
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    if code == 0:
        code = 124
raise SystemExit(code)' "$_timeout_limit" "$@"
    }
fi

export PROGRAM_DIR=${PROGRAM_DIR:-/app}

if ! timeout 120s python3 -m venv "$TMP/venv"; then
    write_reward 0.000000
    write_status "INCOMPLETE: isolated test environment creation failed or timed out"
    exit 1
fi
if ! timeout 120s "$TMP/venv/bin/python" -m pip install --no-index --find-links "$TEST_DIR/wheels" --require-hashes -r "$TEST_DIR/requirements-test.txt"; then
    write_reward 0.000000
    write_status "INCOMPLETE: test dependency installation failed or timed out"
    exit 1
fi

set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout 240s "$TMP/venv/bin/python" -m pytest -q "$TEST_DIR/test_beamline_optics.py" --junitxml="$TMP/results.xml" >"$TMP/out" 2>"$TMP/err"
status=$?
set -e

if [ ! -s "$TMP/results.xml" ]; then
    write_reward 0.000000
    write_status "INCOMPLETE: pytest completion report missing"
else
    chmod 0644 "$REWARD_FILE" 2>/dev/null || true
    chmod 0644 "$STATUS_FILE" 2>/dev/null || true
    python3 - "$TMP/results.xml" "$REWARD_FILE" "$STATUS_FILE" <<'PY'
import math
import sys
import xml.etree.ElementTree as ET

groups = {
    "optics": (0.31, {"test_drift_transport_in_uncovered_gaps", "test_positive_quadrupole_focuses_x_and_defocuses_y", "test_charge_flip_swaps_quadrupole_planes", "test_sequential_matrix_composition_requires_slope_transport", "test_quadrupole_boundary_is_applied_only_inside_interval", "test_quadrupole_zero_gradient_is_drift"}),
    "events": (0.29, {"test_collimator_finds_first_internal_intersection", "test_collimator_entry_boundary_is_immediate_loss", "test_collimator_exit_loss_precedes_same_coordinate_screen", "test_screen_is_intercepting_and_stops_later_transport", "test_screen_inside_quadrupole_uses_partial_matrix", "test_events_are_stably_sorted_independent_of_particle_order"}),
    "diagnostics": (0.17, {"test_diagnostics_use_only_transmitted_particles", "test_population_covariance_and_rms_emittance_are_analytical"}),
    "ledger_rejection": (0.18, {"test_first_batch_creates_ledger_with_matching_totals", "test_second_batch_accumulates_persistent_ledger", "test_duplicate_run_id_rejects_without_any_side_effects", "test_invalid_overlapping_elements_preserve_report_and_ledger", "test_malformed_ledger_and_nonfinite_or_boolean_inputs_preserve_artifacts", "test_overflowing_quadrupole_is_rejected_without_partial_commit"}),
    "output": (0.05, {"test_unwritable_ledger_parent_cleans_prepared_output_temp_and_preserves_output", "test_successful_atomic_replacement_does_not_mutate_symlink_targets"}),
}
expected = set().union(*(tests for _, tests in groups.values()))
passed, seen = set(), []
report_complete = False
weighted_score = 0.0
try:
    root = ET.parse(sys.argv[1]).getroot()
    for case in root.findall(".//testcase"):
        name = case.attrib.get("name", "")
        seen.append(name)
        if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            passed.add(name)
    report_complete = set(seen) == expected and len(seen) == len(expected)
    if report_complete:
        weighted_score = sum(weight * len(tests & passed) / len(tests) for weight, tests in groups.values())
except Exception:
    report_complete = False
    weighted_score = 0.0
if not math.isfinite(weighted_score):
    weighted_score = 0.0
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    handle.write(f"{weighted_score:.6f}\n")
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    if report_complete:
        handle.write("COMPLETE: pytest junit completion report verified\n")
    else:
        handle.write("INCOMPLETE: pytest junit report does not contain the complete expected test set\n")
PY
    chmod 0444 "$REWARD_FILE" 2>/dev/null || true
    chmod 0444 "$STATUS_FILE" 2>/dev/null || true
fi

cat "$TMP/out" 2>/dev/null || true
cat "$TMP/err" >&2 2>/dev/null || true
exit "$status"
