"""Deterministic paraxial beam transport, event handling, and ledger logic."""
from __future__ import annotations

import math
from copy import deepcopy


class InputError(ValueError):
    """Raised for a request or persistent-state error that must be rejected."""


TOTAL_KEYS = ("runs", "transmitted", "absorbed", "screened", "transmitted_charge")
LEDGER_SCHEMA = "beamline-optics-ledger/v1"


def _number(value, label, *, positive=False, nonzero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{label} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise InputError(f"{label} must be finite")
    if positive and value <= 0:
        raise InputError(f"{label} must be positive")
    if nonzero and value == 0:
        raise InputError(f"{label} must be nonzero")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} must be a non-empty string")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise InputError(f"{label} must be an array")
    return value


def _vec2(value, label):
    values = _array(value, label)
    if len(values) != 2:
        raise InputError(f"{label} must have length 2")
    return [_number(values[0], f"{label}[0]"), _number(values[1], f"{label}[1]")]


def _unique_text(items, label):
    seen = set()
    for item in items:
        name = _text(item, label)
        if name in seen:
            raise InputError(f"duplicate {label}: {name}")
        seen.add(name)


def parse_request(raw):
    raw = _object(raw, "input")
    required = {"run_id", "line_end", "particles", "elements", "screens"}
    if set(raw) != required:
        raise InputError("input must contain exactly run_id, line_end, particles, elements, screens")
    run_id = _text(raw["run_id"], "run_id")
    line_end = _number(raw["line_end"], "line_end", positive=True)

    particles = []
    particle_ids = set()
    source_particles = _array(raw["particles"], "particles")
    if not source_particles:
        raise InputError("particles must be non-empty")
    for index, item in enumerate(source_particles):
        item = _object(item, f"particles[{index}]")
        if set(item) != {"id", "rigidity", "charge", "position", "slope"}:
            raise InputError(f"particles[{index}] has unexpected fields")
        particle_id = _text(item["id"], f"particles[{index}].id")
        if particle_id in particle_ids:
            raise InputError(f"duplicate particle id: {particle_id}")
        particle_ids.add(particle_id)
        x, y = _vec2(item["position"], f"particles[{index}].position")
        xp, yp = _vec2(item["slope"], f"particles[{index}].slope")
        particles.append({
            "id": particle_id,
            "rigidity": _number(item["rigidity"], f"particles[{index}].rigidity", positive=True),
            "charge": _number(item["charge"], f"particles[{index}].charge", nonzero=True),
            "x": x, "y": y, "xp": xp, "yp": yp,
        })

    elements = []
    element_ids = set()
    for index, item in enumerate(_array(raw["elements"], "elements")):
        item = _object(item, f"elements[{index}]")
        element_type = item.get("type")
        expected = {"id", "type", "s_start", "s_end"}
        if element_type == "quadrupole":
            expected.add("gradient")
        elif element_type == "collimator":
            expected.add("radius")
        elif element_type != "drift":
            raise InputError(f"elements[{index}].type is invalid")
        if set(item) != expected:
            raise InputError(f"elements[{index}] has incorrect fields")
        element_id = _text(item["id"], f"elements[{index}].id")
        if element_id in element_ids:
            raise InputError(f"duplicate element id: {element_id}")
        element_ids.add(element_id)
        s_start = _number(item["s_start"], f"elements[{index}].s_start")
        s_end = _number(item["s_end"], f"elements[{index}].s_end")
        if not (0 <= s_start < s_end <= line_end):
            raise InputError(f"elements[{index}] interval is outside line")
        parsed = {"id": element_id, "type": element_type, "s_start": s_start, "s_end": s_end}
        if element_type == "quadrupole":
            parsed["gradient"] = _number(item["gradient"], f"elements[{index}].gradient")
        if element_type == "collimator":
            parsed["radius"] = _number(item["radius"], f"elements[{index}].radius", positive=True)
        elements.append(parsed)
    elements.sort(key=lambda e: (e["s_start"], e["s_end"], e["id"]))
    for left, right in zip(elements, elements[1:]):
        if left["s_end"] > right["s_start"]:
            raise InputError("element intervals overlap")

    screens = []
    screen_ids, screen_positions = set(), set()
    for index, item in enumerate(_array(raw["screens"], "screens")):
        item = _object(item, f"screens[{index}]")
        if set(item) != {"id", "s"}:
            raise InputError(f"screens[{index}] has incorrect fields")
        screen_id = _text(item["id"], f"screens[{index}].id")
        if screen_id in screen_ids:
            raise InputError(f"duplicate screen id: {screen_id}")
        screen_ids.add(screen_id)
        s = _number(item["s"], f"screens[{index}].s")
        if not (0 < s <= line_end):
            raise InputError(f"screens[{index}].s is outside line")
        if s in screen_positions:
            raise InputError("screen positions must be unique")
        screen_positions.add(s)
        screens.append({"id": screen_id, "s": s})
    screens.sort(key=lambda item: (item["s"], item["id"]))
    return {"run_id": run_id, "line_end": line_end, "particles": particles, "elements": elements, "screens": screens}


def empty_ledger():
    return {"schema": LEDGER_SCHEMA, "run_ids": [], "totals": {key: 0 for key in TOTAL_KEYS}}


def parse_ledger(raw):
    if raw is None:
        return empty_ledger()
    raw = _object(raw, "ledger")
    if set(raw) != {"schema", "run_ids", "totals"} or raw.get("schema") != LEDGER_SCHEMA:
        raise InputError("ledger schema is invalid")
    run_ids = _array(raw["run_ids"], "ledger.run_ids")
    _unique_text(run_ids, "ledger run id")
    totals = _object(raw["totals"], "ledger.totals")
    if set(totals) != set(TOTAL_KEYS):
        raise InputError("ledger totals keys are invalid")
    parsed_totals = {}
    for key in TOTAL_KEYS:
        value = totals[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise InputError(f"ledger total {key} is invalid")
        if key == "transmitted_charge":
            parsed_totals[key] = float(value)
        elif int(value) != value or value < 0:
            raise InputError(f"ledger total {key} is invalid")
        else:
            parsed_totals[key] = int(value)
    if parsed_totals["runs"] != len(run_ids):
        raise InputError("ledger run count disagrees with run_ids")
    return {"schema": LEDGER_SCHEMA, "run_ids": list(run_ids), "totals": parsed_totals}


def _finite_state(state):
    if not all(math.isfinite(state[key]) for key in ("x", "y", "xp", "yp")):
        raise InputError("non-finite transport state")


def _drift(state, length):
    state["x"] += length * state["xp"]
    state["y"] += length * state["yp"]
    _finite_state(state)


def _plane(u, up, K, length):
    if K == 0:
        return u + length * up, up
    if K > 0:
        root = math.sqrt(K)
        angle = root * length
        return math.cos(angle) * u + math.sin(angle) * up / root, -root * math.sin(angle) * u + math.cos(angle) * up
    root = math.sqrt(-K)
    angle = root * length
    return math.cosh(angle) * u + math.sinh(angle) * up / root, root * math.sinh(angle) * u + math.cosh(angle) * up


def _quadrupole(state, element, length):
    k = state["charge"] * element["gradient"] / state["rigidity"]
    if not math.isfinite(k):
        raise InputError("non-finite quadrupole strength")
    try:
        state["x"], state["xp"] = _plane(state["x"], state["xp"], k, length)
        state["y"], state["yp"] = _plane(state["y"], state["yp"], -k, length)
    except (OverflowError, ValueError) as exc:
        raise InputError("quadrupole numerical overflow") from exc
    _finite_state(state)


def _collimator_root(state, radius, length):
    """Return first t in [0,length] where drift trajectory meets circular aperture."""
    x, y, xp, yp = state["x"], state["y"], state["xp"], state["yp"]
    c = x * x + y * y - radius * radius
    if c >= 0:
        return 0.0
    a = xp * xp + yp * yp
    b = 2.0 * (x * xp + y * yp)
    if a == 0:
        return None
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return None
    root = math.sqrt(max(0.0, disc))
    candidates = [(-b - root) / (2.0 * a), (-b + root) / (2.0 * a)]
    epsilon = 1e-12 * max(1.0, length)
    valid = [t for t in candidates if -epsilon <= t <= length + epsilon]
    if not valid:
        return None
    return min(max(0.0, min(length, t)) for t in valid)


def _element_for(elements, left, right):
    midpoint = (left + right) / 2.0
    for element in elements:
        if element["s_start"] < midpoint < element["s_end"]:
            return element
    return None


def _position(state):
    return [float(state["x"]), float(state["y"])]


def _slope(state):
    return [float(state["xp"]), float(state["yp"])]


def _diagnostics(transmitted, absorbed_count, screened_count):
    count = len(transmitted)
    if count:
        values = [[p["x"], p["xp"], p["y"], p["yp"]] for p in transmitted]
        centroid = [sum(row[j] for row in values) / count for j in range(4)]
    else:
        values, centroid = [], [0.0] * 4
    covariance = [[0.0] * 4 for _ in range(4)]
    if count >= 2:
        for i in range(4):
            for j in range(4):
                covariance[i][j] = sum((row[i] - centroid[i]) * (row[j] - centroid[j]) for row in values) / count
    emit_x = math.sqrt(max(0.0, covariance[0][0] * covariance[1][1] - covariance[0][1] ** 2))
    emit_y = math.sqrt(max(0.0, covariance[2][2] * covariance[3][3] - covariance[2][3] ** 2))
    return {
        "transmitted_count": count,
        "absorbed_count": absorbed_count,
        "screened_count": screened_count,
        "transmitted_charge": float(sum(p["charge"] for p in transmitted)),
        "centroid": centroid,
        "covariance": covariance,
        "rms_emittance": {"x": emit_x, "y": emit_y},
    }


def simulate(raw_request, raw_ledger):
    request = parse_request(raw_request)
    ledger = parse_ledger(raw_ledger)
    if request["run_id"] in ledger["run_ids"]:
        raise InputError("run_id already exists in ledger")

    alive = [deepcopy(p) for p in request["particles"]]
    losses, hits = [], []
    elements = request["elements"]
    screens_at = {screen["s"]: screen for screen in request["screens"]}
    points = sorted({0.0, request["line_end"], *(e["s_start"] for e in elements), *(e["s_end"] for e in elements), *screens_at})
    for left, right in zip(points, points[1:]):
        length = right - left
        element = _element_for(elements, left, right)
        survivors = []
        for state in alive:
            if element and element["type"] == "collimator":
                t = _collimator_root(state, element["radius"], length)
                if t is not None:
                    _drift(state, t)
                    losses.append({"id": state["id"], "element_id": element["id"], "s": float(left + t), "position": _position(state)})
                    continue
                _drift(state, length)
            elif element and element["type"] == "quadrupole":
                _quadrupole(state, element, length)
            else:
                _drift(state, length)
            survivors.append(state)
        alive = survivors
        screen = screens_at.get(right)
        if screen:
            survivors = []
            for state in alive:
                hits.append({"id": state["id"], "screen_id": screen["id"], "s": float(right), "position": _position(state), "slope": _slope(state)})
            alive = survivors

    # Since every screen is an intercepting screen, no particle can cross it.  The
    # loop above nevertheless deliberately keeps the explicit survivor assignment.
    transmitted = sorted(alive, key=lambda p: p["id"])
    losses.sort(key=lambda item: (item["s"], item["id"]))
    hits.sort(key=lambda item: (item["s"], item["id"]))
    diagnostics = _diagnostics(transmitted, len(losses), len(hits))
    totals = deepcopy(ledger["totals"])
    totals["runs"] += 1
    totals["transmitted"] += diagnostics["transmitted_count"]
    totals["absorbed"] += diagnostics["absorbed_count"]
    totals["screened"] += diagnostics["screened_count"]
    totals["transmitted_charge"] += diagnostics["transmitted_charge"]
    if not all(math.isfinite(float(value)) for value in totals.values()):
        raise InputError("non-finite ledger total")
    result = {
        "status": "ok",
        "run_id": request["run_id"],
        "line_end": request["line_end"],
        "particles": [{"id": p["id"], "s": request["line_end"], "position": _position(p), "slope": _slope(p)} for p in transmitted],
        "losses": losses,
        "hits": hits,
        "diagnostics": diagnostics,
        "ledger_totals": totals,
    }
    next_ledger = {"schema": LEDGER_SCHEMA, "run_ids": ledger["run_ids"] + [request["run_id"]], "totals": totals}
    return result, next_ledger
