import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile

PROGRAM_DIR = pathlib.Path(os.environ.get("PROGRAM_DIR", "/app"))


def particle(pid, *, rigidity=1.0, charge=1.0, position=(0.0, 0.0), slope=(0.0, 0.0)):
    return {"id": pid, "rigidity": rigidity, "charge": charge, "position": list(position), "slope": list(slope)}


def request(run_id="run", *, line_end=1.0, particles=None, elements=None, screens=None):
    return {
        "run_id": run_id,
        "line_end": line_end,
        "particles": particles if particles is not None else [particle("p")],
        "elements": elements if elements is not None else [],
        "screens": screens if screens is not None else [],
    }


def invoke(data, *, root=None, output_bytes=None, ledger_bytes=None, output_symlink=False, ledger_symlink=False, output_target_bytes=b"output-target-sentinel", ledger_target_bytes=b"ledger-target-sentinel"):
    """Run the public CLI and return process, decoded response, bytes, and temporary artifacts."""
    context = tempfile.TemporaryDirectory() if root is None else None
    base = pathlib.Path(context.name if context else root)
    base.mkdir(parents=True, exist_ok=True)
    inp, output, ledger = base / "input.json", base / "output.json", base / "ledger.json"
    inp.write_text(json.dumps(data, allow_nan=True), encoding="utf-8")
    if output_bytes is not None:
        output.write_bytes(output_bytes)
    if ledger_bytes is not None:
        ledger.write_bytes(ledger_bytes)
    targets = []
    if output_symlink:
        target = base / "output-target.bin"; target.write_bytes(output_target_bytes); output.unlink(missing_ok=True); output.symlink_to(target); targets.append(target)
    if ledger_symlink:
        target = base / "ledger-target.bin"; target.write_bytes(ledger_target_bytes); ledger.unlink(missing_ok=True); ledger.symlink_to(target); targets.append(target)
    env = os.environ.copy(); env["PYTHONPATH"] = str(PROGRAM_DIR)
    proc = subprocess.run([sys.executable, "-m", "beamline_optics", "--input", str(inp), "--output", str(output), "--ledger", str(ledger)], cwd=PROGRAM_DIR, env=env, text=True, capture_output=True)
    response = json.loads(output.read_text(encoding="utf-8")) if proc.returncode == 0 else None
    output_raw = output.read_bytes() if output.exists() else None
    ledger_raw = ledger.read_bytes() if ledger.exists() else None
    temps = sorted(path.name for path in base.glob(".beam-*"))
    outcome = proc, response, output_raw, ledger_raw, temps, output, ledger, targets
    if context:
        context.cleanup()
    return outcome


def transmitted(result, pid):
    return next(item for item in result["particles"] if item["id"] == pid)


def assert_rejected(data, *, ledger_bytes=None):
    """Assert rejection after a valid commit, including every observable side effect."""
    with tempfile.TemporaryDirectory() as td:
        bootstrap, _, _, original_ledger, _, *_ = invoke(request("rejection-precondition"), root=td)
        assert bootstrap.returncode == 0
        expected_ledger = original_ledger if ledger_bytes is None else ledger_bytes
        proc, result, output_raw, next_ledger_raw, temps, *_ = invoke(data, root=td, output_bytes=b"OUTPUT-SENTINEL\n", ledger_bytes=ledger_bytes)
        assert proc.returncode != 0
        assert proc.stderr.startswith("INPUT_ERROR:")
        assert result is None
        assert output_raw == b"OUTPUT-SENTINEL\n"
        assert next_ledger_raw == expected_ledger
        assert temps == []


def test_drift_transport_in_uncovered_gaps():
    """R1：未被元件覆盖的束线区间也必须按漂移传输。"""
    data = request(line_end=4.0, particles=[particle("p", position=(1, -2), slope=(0.5, 1.0))], elements=[{"id": "declared-drift", "type": "drift", "s_start": 1.0, "s_end": 2.0}])
    proc, result, *_ = invoke(data)
    state = transmitted(result, "p")
    assert proc.returncode == 0
    assert state["s"] == 4.0
    assert state["position"] == [3.0, 2.0]
    assert state["slope"] == [0.5, 1.0]


def test_positive_quadrupole_focuses_x_and_defocuses_y():
    """R2：正 k 的有限长四极铁必须使 x 聚焦、y 散焦。"""
    data = request(line_end=0.5, particles=[particle("p", position=(1, 1))], elements=[{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 0.5, "gradient": 1.0}])
    proc, result, *_ = invoke(data)
    x, y = transmitted(result, "p")["position"]
    assert proc.returncode == 0
    assert math.isclose(x, math.cos(0.5), rel_tol=0, abs_tol=1e-12)
    assert math.isclose(y, math.cosh(0.5), rel_tol=0, abs_tol=1e-12)
    assert abs(x) < 1.0 < abs(y)


def test_charge_flip_swaps_quadrupole_planes():
    """R2：电荷符号反转必须交换两个横向平面的聚焦响应。"""
    element = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 0.5, "gradient": 1.0}]
    _, positive, *_ = invoke(request("positive", line_end=0.5, particles=[particle("p", charge=1, position=(1, 1))], elements=element))
    _, negative, *_ = invoke(request("negative", line_end=0.5, particles=[particle("p", charge=-1, position=(1, 1))], elements=element))
    px, py = transmitted(positive, "p")["position"]
    nx, ny = transmitted(negative, "p")["position"]
    assert math.isclose(px, ny, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(py, nx, rel_tol=0, abs_tol=1e-12)
    assert px < py and nx > ny


def test_sequential_matrix_composition_requires_slope_transport():
    """R3：四极铁后的漂移必须使用已改变的斜率，不能只变换位置。"""
    elements = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 1.0, "gradient": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements))
    state = transmitted(result, "p")
    expected_x = math.cos(1.0) - math.sin(1.0)
    assert proc.returncode == 0
    assert math.isclose(state["position"][0], expected_x, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(state["slope"][0], -math.sin(1.0), rel_tol=0, abs_tol=1e-12)


def test_quadrupole_boundary_is_applied_only_inside_interval():
    """R3：屏幕恰在四极铁入口时先完成左侧漂移，入口场不能提前作用。"""
    elements = [{"id": "q", "type": "quadrupole", "s_start": 1.0, "s_end": 2.0, "gradient": 4.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(0, 0), slope=(1, 0))], elements=elements, screens=[{"id": "entry", "s": 1.0}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert hit["s"] == 1.0
    assert hit["position"] == [1.0, 0.0]
    assert hit["slope"] == [1.0, 0.0]


def test_quadrupole_zero_gradient_is_drift():
    """R3：零梯度四极铁是精确漂移而非数值奇异点。"""
    element = [{"id": "q0", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 0.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, -1), slope=(0.25, -0.5))], elements=element))
    assert proc.returncode == 0
    assert transmitted(result, "p")["position"] == [1.5, -2.0]


def test_collimator_finds_first_internal_intersection():
    """R4：准直器必须记录段内首次圆孔交点，而不是只看段末。"""
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 2.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", slope=(1, 0))], elements=elements))
    loss = result["losses"][0]
    assert proc.returncode == 0
    assert result["particles"] == []
    assert math.isclose(loss["s"], 1.0, abs_tol=1e-12)
    assert loss["position"] == [1.0, 0.0]


def test_collimator_entry_boundary_is_immediate_loss():
    """R4：粒子在准直器入口已位于孔径边界时必须立刻损失。"""
    elements = [{"id": "ap", "type": "collimator", "s_start": 1.0, "s_end": 2.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements))
    assert proc.returncode == 0
    assert result["losses"] == [{"id": "p", "element_id": "ap", "s": 1.0, "position": [1.0, 0.0]}]


def test_collimator_exit_loss_precedes_same_coordinate_screen():
    """R5：准直器出口孔径边界的损失优先于同 s 的截获屏。"""
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=1.0, particles=[particle("p", slope=(1, 0))], elements=elements, screens=[{"id": "end", "s": 1.0}]))
    assert proc.returncode == 0
    assert len(result["losses"]) == 1 and result["losses"][0]["s"] == 1.0
    assert result["hits"] == []
    assert result["diagnostics"]["absorbed_count"] == 1


def test_screen_is_intercepting_and_stops_later_transport():
    """R5：屏幕命中后粒子不得继续进入后续束线或最终透射集合。"""
    elements = [{"id": "after", "type": "quadrupole", "s_start": 1.0, "s_end": 2.0, "gradient": 3.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", slope=(1, 0))], elements=elements, screens=[{"id": "screen", "s": 1.0}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert result["particles"] == []
    assert hit["position"] == [1.0, 0.0]
    assert hit["slope"] == [1.0, 0.0]


def test_screen_inside_quadrupole_uses_partial_matrix():
    """R6：位于四极铁内部的屏幕必须接收至该精确坐标的部分矩阵传播。"""
    elements = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements, screens=[{"id": "inside", "s": 0.5}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert math.isclose(hit["position"][0], math.cos(0.5), abs_tol=1e-12)
    assert math.isclose(hit["slope"][0], -math.sin(0.5), abs_tol=1e-12)


def test_events_are_stably_sorted_independent_of_particle_order():
    """R6：同坐标事件按粒子 ID 排序，输入顺序不应改变可观察事件顺序。"""
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    ps = [particle("z", slope=(1, 0)), particle("a", slope=(1, 0))]
    _, first, *_ = invoke(request("sort-a", line_end=1.0, particles=ps, elements=elements))
    _, second, *_ = invoke(request("sort-b", line_end=1.0, particles=list(reversed(ps)), elements=elements))
    assert [loss["id"] for loss in first["losses"]] == ["a", "z"]
    assert first["losses"] == second["losses"]


def test_diagnostics_use_only_transmitted_particles():
    """R7：质心、电荷和计数只能使用最终透射粒子，不能混入损失或屏幕命中。"""
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    ps = [particle("lost", charge=9, slope=(2, 0)), particle("live", charge=-2, position=(0.2, 0), slope=(0.1, 0))]
    proc, result, *_ = invoke(request(line_end=1.0, particles=ps, elements=elements))
    diag = result["diagnostics"]
    assert proc.returncode == 0
    assert diag["transmitted_count"] == 1 and diag["absorbed_count"] == 1
    assert diag["transmitted_charge"] == -2.0
    assert diag["centroid"] == [0.30000000000000004, 0.1, 0.0, 0.0]


def test_population_covariance_and_rms_emittance_are_analytical():
    """R7：四粒已知束团的总体协方差和 RMS 发射度必须与解析值一致。"""
    ps = [particle("a", position=(1, 1), slope=(0, 0)), particle("b", position=(-1, -1), slope=(0, 0)), particle("c", position=(0, 0), slope=(1, 1)), particle("d", position=(0, 0), slope=(-1, -1))]
    proc, result, *_ = invoke(request(line_end=0.000001, particles=ps))
    diag = result["diagnostics"]
    assert proc.returncode == 0
    assert math.isclose(diag["covariance"][0][0], 0.5, abs_tol=2e-6)
    assert math.isclose(diag["covariance"][1][1], 0.5, abs_tol=2e-6)
    assert math.isclose(diag["covariance"][0][1], 0.0, abs_tol=2e-6)
    assert math.isclose(diag["rms_emittance"]["x"], 0.5, abs_tol=2e-6)
    assert math.isclose(diag["rms_emittance"]["y"], 0.5, abs_tol=2e-6)


def test_first_batch_creates_ledger_with_matching_totals():
    """R8：首次成功运行必须创建账本，且输出累计值与账本逐项一致。"""
    with tempfile.TemporaryDirectory() as td:
        proc, result, _, ledger_raw, temps, *_ = invoke(request("batch-1", line_end=1.0, particles=[particle("p", charge=3)]), root=td)
        ledger = json.loads(ledger_raw)
        assert proc.returncode == 0 and temps == []
        assert ledger["run_ids"] == ["batch-1"]
        assert result["ledger_totals"] == ledger["totals"]
        assert ledger["totals"] == {"runs": 1, "transmitted": 1, "absorbed": 0, "screened": 0, "transmitted_charge": 3.0}


def test_second_batch_accumulates_persistent_ledger():
    """R8：不同 run_id 的第二批必须在同一账本上累加物理流量。"""
    with tempfile.TemporaryDirectory() as td:
        first, _, _, _, _, *_ = invoke(request("one", line_end=1.0, particles=[particle("p", charge=2)]), root=td)
        elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
        second, result, _, ledger_raw, _, *_ = invoke(request("two", line_end=1.0, particles=[particle("lost", slope=(2, 0))], elements=elements), root=td)
        totals = json.loads(ledger_raw)["totals"]
        assert first.returncode == 0 and second.returncode == 0
        assert totals == {"runs": 2, "transmitted": 1, "absorbed": 1, "screened": 0, "transmitted_charge": 2.0}
        assert result["ledger_totals"] == totals


def test_duplicate_run_id_rejects_without_any_side_effects():
    """R9：重复批次必须拒收，并保持已有报告、账本和临时目录不变。"""
    with tempfile.TemporaryDirectory() as td:
        first, _, output_raw, ledger_raw, _, *_ = invoke(request("once"), root=td)
        second, result, after_output, after_ledger, temps, *_ = invoke(request("once"), root=td)
        assert first.returncode == 0 and second.returncode != 0
        assert second.stderr.startswith("INPUT_ERROR:") and result is None
        assert after_output == output_raw and after_ledger == ledger_raw and temps == []


def test_invalid_overlapping_elements_preserve_report_and_ledger():
    """R9：重叠元件是业务校验失败，必须不改写报告、账本或留下临时文件。"""
    elements = [{"id": "a", "type": "drift", "s_start": 0, "s_end": 1}, {"id": "b", "type": "quadrupole", "s_start": 0.5, "s_end": 1.5, "gradient": 1}]
    assert_rejected(request(line_end=2.0, elements=elements))


def test_malformed_ledger_and_nonfinite_or_boolean_inputs_preserve_artifacts():
    """R9：损坏账本、非有限数和布尔伪数值均须通过完整副作用断言拒收。"""
    assert_rejected(request("bad-ledger"), ledger_bytes=b"not-json\n")
    assert_rejected(request("nan", particles=[particle("p", rigidity=float("nan"))]))
    assert_rejected(request("bool", line_end=True))


def test_overflowing_quadrupole_is_rejected_without_partial_commit():
    """R10：有限输入若导致双曲矩阵溢出，必须走 INPUT_ERROR 且保持所有持久化对象。"""
    elements = [{"id": "huge", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 1e308}]
    assert_rejected(request("overflow", line_end=2.0, particles=[particle("p")], elements=elements))


def test_unwritable_ledger_parent_cleans_prepared_output_temp_and_preserves_output():
    """R10：账本目标目录不存在时，已准备的输出临时文件必须清理且旧报告不能被改写。"""
    with tempfile.TemporaryDirectory() as td:
        bootstrap, *_ = invoke(request("io-precondition"), root=td)
        assert bootstrap.returncode == 0
        base = pathlib.Path(td)
        inp, output, ledger = base / "input.json", base / "output.json", base / "missing" / "ledger.json"
        inp.write_text(json.dumps(request("io-failure")), encoding="utf-8")
        output.write_bytes(b"OLD-OUTPUT-BYTES\n")
        env = os.environ.copy(); env["PYTHONPATH"] = str(PROGRAM_DIR)
        proc = subprocess.run([sys.executable, "-m", "beamline_optics", "--input", str(inp), "--output", str(output), "--ledger", str(ledger)], cwd=PROGRAM_DIR, env=env, text=True, capture_output=True)
        assert proc.returncode != 0 and proc.stderr.startswith("INPUT_ERROR:")
        assert output.read_bytes() == b"OLD-OUTPUT-BYTES\n"
        assert not ledger.exists() and not ledger.parent.exists()
        assert list(base.glob(".beam-*")) == []


def test_successful_atomic_replacement_does_not_mutate_symlink_targets():
    """R10：成功提交必须替换输出和账本路径本身，不能沿符号链接改写旧目标。"""
    empty_ledger = b'{"schema":"beamline-optics-ledger/v1","run_ids":[],"totals":{"runs":0,"transmitted":0,"absorbed":0,"screened":0,"transmitted_charge":0}}\n'
    with tempfile.TemporaryDirectory() as td:
        proc, result, output_raw, ledger_raw, temps, output, ledger, targets = invoke(request("atomic"), root=td, output_symlink=True, ledger_symlink=True, ledger_target_bytes=empty_ledger)
        assert proc.returncode == 0 and result["status"] == "ok"
        assert not output.is_symlink() and not ledger.is_symlink()
        assert [target.read_bytes() for target in targets] == [b"output-target-sentinel", empty_ledger]
        assert output_raw.endswith(b"\n") and ledger_raw.endswith(b"\n") and temps == []
