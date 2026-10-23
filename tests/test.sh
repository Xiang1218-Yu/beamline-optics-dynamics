#!/bin/sh
set -u
TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REWARD_FILE=${REWARD_FILE:-/logs/verifier/reward.txt}
mkdir -p "$(dirname "$REWARD_FILE")"
printf '0.000000\n' > "$REWARD_FILE"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export PROGRAM_DIR=${PROGRAM_DIR:-/app}
python3 -m venv "$TMP/venv" || exit 1
"$TMP/venv/bin/python" -m pip install --no-index --find-links "$TEST_DIR/wheels" --require-hashes -r "$TEST_DIR/requirements-test.txt" || exit 1
set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$TMP/venv/bin/python" -m pytest -q "$TEST_DIR/test_beamline_optics.py" --junitxml="$TMP/results.xml" >"$TMP/out" 2>"$TMP/err"
status=$?
set -e
python3 - "$TMP/results.xml" "$REWARD_FILE" <<'PY'
import math, sys, xml.etree.ElementTree as E
groups = {
 "optics": (0.31, {"test_drift_transport_in_uncovered_gaps", "test_positive_quadrupole_focuses_x_and_defocuses_y", "test_charge_flip_swaps_quadrupole_planes", "test_sequential_matrix_composition_requires_slope_transport", "test_quadrupole_boundary_is_applied_only_inside_interval", "test_quadrupole_zero_gradient_is_drift"}),
 "events": (0.29, {"test_collimator_finds_first_internal_intersection", "test_collimator_entry_boundary_is_immediate_loss", "test_collimator_exit_loss_precedes_same_coordinate_screen", "test_screen_is_intercepting_and_stops_later_transport", "test_screen_inside_quadrupole_uses_partial_matrix", "test_events_are_stably_sorted_independent_of_particle_order"}),
 "diagnostics": (0.17, {"test_diagnostics_use_only_transmitted_particles", "test_population_covariance_and_rms_emittance_are_analytical"}),
 "ledger_rejection": (0.18, {"test_first_batch_creates_ledger_with_matching_totals", "test_second_batch_accumulates_persistent_ledger", "test_duplicate_run_id_rejects_without_any_side_effects", "test_invalid_overlapping_elements_preserve_report_and_ledger", "test_malformed_ledger_and_nonfinite_or_boolean_inputs_preserve_artifacts", "test_overflowing_quadrupole_is_rejected_without_partial_commit"}),
 "output": (0.05, {"test_unwritable_ledger_parent_cleans_prepared_output_temp_and_preserves_output", "test_successful_atomic_replacement_does_not_mutate_symlink_targets"}),
}
expected = set().union(*(tests for _, tests in groups.values()))
passed, seen = set(), []
try:
    root = E.parse(sys.argv[1]).getroot()
    for case in root.findall('.//testcase'):
        name = case.attrib.get('name', '')
        seen.append(name)
        if not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')):
            passed.add(name)
    score = sum(weight * len(tests & passed) / len(tests) for weight, tests in groups.values()) if set(seen) == expected and len(seen) == len(expected) else 0.0
except Exception:
    score = 0.0
with open(sys.argv[2], 'w', encoding='utf-8') as handle:
    handle.write(f'{score if math.isfinite(score) else 0.0:.6f}\n')
PY
cat "$TMP/out"
cat "$TMP/err" >&2
exit "$status"
