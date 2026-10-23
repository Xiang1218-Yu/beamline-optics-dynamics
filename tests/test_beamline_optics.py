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


def rejection_effects(data, *, ledger_bytes=None):
    """Run a rejection after a valid commit and return every observable side effect."""
    with tempfile.TemporaryDirectory() as td:
        bootstrap, _, _, original_ledger, _, *_ = invoke(request("rejection-precondition"), root=td)
        assert bootstrap.returncode == 0
        expected_ledger = original_ledger if ledger_bytes is None else ledger_bytes
        proc, result, output_raw, next_ledger_raw, temps, *_ = invoke(
            data,
            root=td,
            output_bytes=b"OUTPUT-SENTINEL\n",
            ledger_bytes=ledger_bytes,
        )
        return proc, result, output_raw, next_ledger_raw, temps, expected_ledger


def test_drift_transport_in_uncovered_gaps():
    """Case ID: BLO-R1-01
    题面原文：“未被元件覆盖的区间按漂移段处理。”
    行为：显式漂移元件之外的区间仍按同一漂移传输规则推进。
    """
    data = request(line_end=4.0, particles=[particle("p", position=(1, -2), slope=(0.5, 1.0))], elements=[{"id": "declared-drift", "type": "drift", "s_start": 1.0, "s_end": 2.0}])
    proc, result, *_ = invoke(data)
    state = transmitted(result, "p")
    assert proc.returncode == 0
    assert state["s"] == 4.0
    assert state["position"] == [3.0, 2.0]
    assert state["slope"] == [0.5, 1.0]


def test_positive_quadrupole_focuses_x_and_defocuses_y():
    """Case ID: BLO-R2-01
    题面原文：“在有限长四极铁中，先计算 `k = charge * gradient / rigidity`。`x` 平面使用 `K=k`，`y` 平面使用 `K=-k`。”
    行为：正梯度四极铁使 x 平面聚焦、y 平面散焦。
    """
    data = request(line_end=0.5, particles=[particle("p", position=(1, 1))], elements=[{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 0.5, "gradient": 1.0}])
    proc, result, *_ = invoke(data)
    x, y = transmitted(result, "p")["position"]
    assert proc.returncode == 0
    assert math.isclose(x, math.cos(0.5), rel_tol=0, abs_tol=1e-12)
    assert math.isclose(y, math.cosh(0.5), rel_tol=0, abs_tol=1e-12)
    assert abs(x) < 1.0 < abs(y)


def test_charge_flip_swaps_quadrupole_planes():
    """Case ID: BLO-R2-02
    题面原文：“改变电荷或梯度符号会交换两个平面的响应。”
    行为：电荷符号反转后，两个横向平面的响应互换。
    """
    element = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 0.5, "gradient": 1.0}]
    _, positive, *_ = invoke(request("positive", line_end=0.5, particles=[particle("p", charge=1, position=(1, 1))], elements=element))
    _, negative, *_ = invoke(request("negative", line_end=0.5, particles=[particle("p", charge=-1, position=(1, 1))], elements=element))
    px, py = transmitted(positive, "p")["position"]
    nx, ny = transmitted(negative, "p")["position"]
    assert math.isclose(px, ny, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(py, nx, rel_tol=0, abs_tol=1e-12)
    assert px < py and nx > ny


def test_sequential_matrix_composition_requires_slope_transport():
    """Case ID: BLO-R3-01
    题面原文：“对每个平面独立应用传输：”
    行为：四极铁后的漂移必须使用四极铁已改变的斜率做矩阵组合。
    """
    elements = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 1.0, "gradient": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements))
    state = transmitted(result, "p")
    expected_x = math.cos(1.0) - math.sin(1.0)
    assert proc.returncode == 0
    assert math.isclose(state["position"][0], expected_x, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(state["slope"][0], -math.sin(1.0), rel_tol=0, abs_tol=1e-12)


def test_quadrupole_boundary_is_applied_only_inside_interval():
    """Case ID: BLO-R3-02
    题面原文：“所有位置相同的边界先完成左侧区间的传输；”
    行为：屏幕位于四极铁入口时，只完成入口左侧的传输，入口场不能提前作用。
    """
    elements = [{"id": "q", "type": "quadrupole", "s_start": 1.0, "s_end": 2.0, "gradient": 4.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(0, 0), slope=(1, 0))], elements=elements, screens=[{"id": "entry", "s": 1.0}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert hit["s"] == 1.0
    assert hit["position"] == [1.0, 0.0]
    assert hit["slope"] == [1.0, 0.0]


def test_quadrupole_zero_gradient_is_drift():
    """Case ID: BLO-R3-03
    题面原文：“K = 0:  按漂移段处理”
    行为：零梯度四极铁是精确漂移，不产生除零或其他数值奇异。
    """
    element = [{"id": "q0", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 0.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, -1), slope=(0.25, -0.5))], elements=element))
    assert proc.returncode == 0
    assert transmitted(result, "p")["position"] == [1.5, -2.0]


def test_collimator_finds_first_internal_intersection():
    """Case ID: BLO-R4-01
    题面原文：“否则必须求出在该段内**首次**满足 `x(s)²+y(s)² = radius²` 的位置；不能只检查段末位置。”
    行为：准直器记录段内第一次穿过圆孔边界的位置，而不是只检查段末。
    """
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 2.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", slope=(1, 0))], elements=elements))
    loss = result["losses"][0]
    assert proc.returncode == 0
    assert result["particles"] == []
    assert math.isclose(loss["s"], 1.0, abs_tol=1e-12)
    assert loss["position"] == [1.0, 0.0]


def test_collimator_entry_boundary_is_immediate_loss():
    """Case ID: BLO-R4-02
    题面原文：“进入准直器时若 `x²+y² >= radius²`，粒子立刻在 `s_start` 被吸收。”
    行为：粒子在准直器入口已位于孔径边界时立即损失。
    """
    elements = [{"id": "ap", "type": "collimator", "s_start": 1.0, "s_end": 2.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements))
    assert proc.returncode == 0
    assert result["losses"] == [{"id": "p", "element_id": "ap", "s": 1.0, "position": [1.0, 0.0]}]


def test_collimator_exit_loss_precedes_same_coordinate_screen():
    """Case ID: BLO-R5-01
    题面原文：“所有位置相同的边界先完成左侧区间的传输；若粒子恰好在准直器出口达到孔径边界，它先被准直器吸收，不能再被同一 `s` 处的屏幕记录。”
    行为：准直器出口损失优先于同一 s 的截获屏命中。
    """
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    proc, result, *_ = invoke(request(line_end=1.0, particles=[particle("p", slope=(1, 0))], elements=elements, screens=[{"id": "end", "s": 1.0}]))
    assert proc.returncode == 0
    assert len(result["losses"]) == 1 and result["losses"][0]["s"] == 1.0
    assert result["hits"] == []
    assert result["diagnostics"]["absorbed_count"] == 1


def test_screen_is_intercepting_and_stops_later_transport():
    """Case ID: BLO-R5-02
    题面原文：“屏幕是截获式诊断屏：粒子抵达屏幕后记录命中并停止继续传输。”
    行为：屏幕命中后粒子停止传输，不进入后续元件或最终透射集合。
    """
    elements = [{"id": "after", "type": "quadrupole", "s_start": 1.0, "s_end": 2.0, "gradient": 3.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", slope=(1, 0))], elements=elements, screens=[{"id": "screen", "s": 1.0}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert result["particles"] == []
    assert hit["position"] == [1.0, 0.0]
    assert hit["slope"] == [1.0, 0.0]


def test_screen_inside_quadrupole_uses_partial_matrix():
    """Case ID: BLO-R6-01
    题面原文：“**R6**：在元件内部的事件平面上做精确部分传输，并稳定排序同位事件。”
    行为：位于四极铁内部的屏幕接收至该精确 s 坐标的部分矩阵传播结果。
    """
    elements = [{"id": "q", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 1.0}]
    proc, result, *_ = invoke(request(line_end=2.0, particles=[particle("p", position=(1, 0))], elements=elements, screens=[{"id": "inside", "s": 0.5}]))
    hit = result["hits"][0]
    assert proc.returncode == 0
    assert math.isclose(hit["position"][0], math.cos(0.5), abs_tol=1e-12)
    assert math.isclose(hit["slope"][0], -math.sin(0.5), abs_tol=1e-12)


def test_events_are_stably_sorted_independent_of_particle_order():
    """Case ID: BLO-R6-02
    题面原文：“同一位置的损失按 `id` 字典序排列，所有损失按 `(s, id)` 排列。”
    题面原文：“屏幕命中按 `(s, id)` 排列。屏幕命中的粒子不会出现在最终 `particles` 中。”
    行为：同位事件按粒子 ID 稳定排序，输入粒子顺序不改变可观察事件顺序。
    """
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    ps = [particle("z", slope=(1, 0)), particle("a", slope=(1, 0))]
    _, first, *_ = invoke(request("sort-a", line_end=1.0, particles=ps, elements=elements))
    _, second, *_ = invoke(request("sort-b", line_end=1.0, particles=list(reversed(ps)), elements=elements))
    assert [loss["id"] for loss in first["losses"]] == ["a", "z"]
    assert first["losses"] == second["losses"]


def test_diagnostics_use_only_transmitted_particles():
    """Case ID: BLO-R7-01
    题面原文：“`diagnostics` 必须包含：”
    题面原文：“`transmitted_charge`，仅统计最终透射粒子的电荷；”
    行为：计数、电荷和质心只使用最终透射粒子，不混入损失或屏幕命中。
    """
    elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
    ps = [particle("lost", charge=9, slope=(2, 0)), particle("live", charge=-2, position=(0.2, 0), slope=(0.1, 0))]
    proc, result, *_ = invoke(request(line_end=1.0, particles=ps, elements=elements))
    diag = result["diagnostics"]
    assert proc.returncode == 0
    assert diag["transmitted_count"] == 1 and diag["absorbed_count"] == 1
    assert diag["transmitted_charge"] == -2.0
    assert diag["centroid"] == [0.30000000000000004, 0.1, 0.0, 0.0]


def test_population_covariance_and_rms_emittance_are_analytical():
    """Case ID: BLO-R7-02
    题面原文：“`covariance`：最终透射粒子的 4×4 总体协方差矩阵，变量顺序与 `centroid` 相同；少于两个透射粒子时为全零矩阵；”
    题面原文：“- **R7**：只用最终透射粒子计算束流诊断、协方差和 RMS 发射度。”
    行为：四粒已知束团的总体协方差和 RMS 发射度与解析值一致。
    """
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
    """Case ID: BLO-R8-01
    题面原文：“## 诊断、账本与写入语义”
    题面原文：“成功输出必须是 UTF-8 JSON，键集合固定为 `status`、`run_id`、`line_end`、`particles`、`losses`、`hits`、`diagnostics`、`ledger_totals`。”
    题面原文：“账本 JSON 的**键集合必须恰好**为 `schema`、`run_ids`、`totals`，并使用下列格式：”
    行为：首次成功运行创建精确结构的账本，输出累计值与账本 totals 逐项一致。
    """
    with tempfile.TemporaryDirectory() as td:
        proc, result, _, ledger_raw, temps, *_ = invoke(request("batch-1", line_end=1.0, particles=[particle("p", charge=3)]), root=td)
        ledger = json.loads(ledger_raw)
        assert proc.returncode == 0 and temps == []
        assert ledger["run_ids"] == ["batch-1"]
        assert result["ledger_totals"] == ledger["totals"]
        assert ledger["totals"] == {"runs": 1, "transmitted": 1, "absorbed": 0, "screened": 0, "transmitted_charge": 3.0}


def test_second_batch_accumulates_persistent_ledger():
    """Case ID: BLO-R8-02
    题面原文：“每次成功运行必须把本次唯一的 `run_id` 追加到 `run_ids` 的末尾，并更新累计 `runs`、`transmitted`、`absorbed`、`screened` 和 `transmitted_charge`。”
    行为：不同 run_id 的第二批在同一账本上累加物理流量。
    """
    with tempfile.TemporaryDirectory() as td:
        first, _, _, _, _, *_ = invoke(request("one", line_end=1.0, particles=[particle("p", charge=2)]), root=td)
        elements = [{"id": "ap", "type": "collimator", "s_start": 0.0, "s_end": 1.0, "radius": 1.0}]
        second, result, _, ledger_raw, _, *_ = invoke(request("two", line_end=1.0, particles=[particle("lost", slope=(2, 0))], elements=elements), root=td)
        totals = json.loads(ledger_raw)["totals"]
        assert first.returncode == 0 and second.returncode == 0
        assert totals == {"runs": 2, "transmitted": 1, "absorbed": 1, "screened": 0, "transmitted_charge": 2.0}
        assert result["ledger_totals"] == totals


def test_duplicate_run_id_rejects_without_any_side_effects():
    """Case ID: BLO-R9-01
    题面原文：“重复 `run_id` 必须拒收。”
    行为：重复批次被拒收；错误写入题面授权的 stderr 通道，已有报告、账本和临时目录不变。
    """
    with tempfile.TemporaryDirectory() as td:
        first, _, output_raw, ledger_raw, _, *_ = invoke(request("once"), root=td)
        second, result, after_output, after_ledger, temps, *_ = invoke(request("once"), root=td)
        assert first.returncode == 0 and second.returncode != 0
        assert second.stderr.startswith("INPUT_ERROR:") and result is None
        assert after_output == output_raw and after_ledger == ledger_raw and temps == []


def test_invalid_overlapping_elements_preserve_report_and_ledger():
    """Case ID: BLO-R9-02
    题面原文：“不同元件的有效区间不得重叠。”
    行为：重叠元件是业务校验失败，必须完整拒收且不改写报告、账本或留下临时文件。
    """
    elements = [{"id": "a", "type": "drift", "s_start": 0, "s_end": 1}, {"id": "b", "type": "quadrupole", "s_start": 0.5, "s_end": 1.5, "gradient": 1}]
    proc, result, output_raw, ledger_raw, temps, expected_ledger = rejection_effects(request(line_end=2.0, elements=elements))
    assert proc.returncode != 0
    assert proc.stderr.startswith("INPUT_ERROR:")
    assert result is None
    assert output_raw == b"OUTPUT-SENTINEL\n"
    assert ledger_raw == expected_ledger
    assert temps == []


def test_malformed_ledger_and_nonfinite_or_boolean_inputs_preserve_artifacts():
    """Case ID: BLO-R9-03
    题面原文：“输入 JSON 必须是对象，包含以下字段：”
    题面原文：“已有账本必须满足该精确结构”
    行为：损坏账本、非有限数和布尔伪数值均通过完整拒收副作用断言。
    """
    cases = [
        (request("bad-ledger"), b"not-json\n"),
        (request("nan", particles=[particle("p", rigidity=float("nan"))]), None),
        (request("bool", line_end=True), None),
    ]
    for data, ledger_bytes in cases:
        proc, result, output_raw, ledger_raw, temps, expected_ledger = rejection_effects(data, ledger_bytes=ledger_bytes)
        assert proc.returncode != 0
        assert proc.stderr.startswith("INPUT_ERROR:")
        assert result is None
        assert output_raw == b"OUTPUT-SENTINEL\n"
        assert ledger_raw == expected_ledger
        assert temps == []


def test_overflowing_quadrupole_is_rejected_without_partial_commit():
    """Case ID: BLO-R10-01
    题面原文：“任何产生非有限中间状态或输出状态的计算都必须作为输入/数值错误拒收，且不能修改已有输出或账本。”
    行为：有限输入导致双曲矩阵溢出时走题面授权的错误通道，且不产生部分提交。
    """
    elements = [{"id": "huge", "type": "quadrupole", "s_start": 0.0, "s_end": 2.0, "gradient": 1e308}]
    proc, result, output_raw, ledger_raw, temps, expected_ledger = rejection_effects(request("overflow", line_end=2.0, particles=[particle("p")], elements=elements))
    assert proc.returncode != 0
    assert proc.stderr.startswith("INPUT_ERROR:")
    assert result is None
    assert output_raw == b"OUTPUT-SENTINEL\n"
    assert ledger_raw == expected_ledger
    assert temps == []


def test_unwritable_ledger_parent_cleans_prepared_output_temp_and_preserves_output():
    """Case ID: BLO-R10-02
    题面原文：“已有 `--output` 与 `--ledger` 文件均逐字节不变，且对应目录中不能遗留临时文件。”
    行为：账本目标目录不可用时，已准备的输出临时文件被清理且旧报告保持不变。
    """
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
    """Case ID: BLO-R10-03
    题面原文：“**R10**：通过同目录临时文件与原子替换提交输出和账本，处理数值溢出与既有路径。”
    行为：成功提交替换输出和账本路径本身，不沿符号链接改写旧目标。
    """
    empty_ledger = b'{"schema":"beamline-optics-ledger/v1","run_ids":[],"totals":{"runs":0,"transmitted":0,"absorbed":0,"screened":0,"transmitted_charge":0}}\n'
    with tempfile.TemporaryDirectory() as td:
        proc, result, output_raw, ledger_raw, temps, output, ledger, targets = invoke(request("atomic"), root=td, output_symlink=True, ledger_symlink=True, ledger_target_bytes=empty_ledger)
        assert proc.returncode == 0 and result["status"] == "ok"
        assert not output.is_symlink() and not ledger.is_symlink()
        assert [target.read_bytes() for target in targets] == [b"output-target-sentinel", empty_ledger]
        assert output_raw.endswith(b"\n") and ledger_raw.endswith(b"\n") and temps == []
