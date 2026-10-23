"""CLI entry point with all-or-nothing persistence for the reference solution."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .core import InputError, simulate


def _load_json(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} is not valid JSON") from exc


def _encoded_json(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InputError("result cannot be encoded as finite JSON") from exc


def _temporary_bytes(path, payload):
    directory = Path(path).parent
    if not directory.exists() or not directory.is_dir():
        raise InputError(f"parent directory for {path} does not exist")
    try:
        fd, temporary = tempfile.mkstemp(prefix=".beam-", dir=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except OSError as exc:
        raise InputError(f"cannot prepare {path}: {exc}") from exc


def _replace_pair(output, output_payload, ledger, ledger_payload):
    prepared = []
    try:
        prepared.append((_temporary_bytes(output, output_payload), output))
        prepared.append((_temporary_bytes(ledger, ledger_payload), ledger))
        for temporary, destination in prepared:
            os.replace(temporary, destination)
            temporary = None
    except OSError as exc:
        raise InputError(f"cannot atomically persist results: {exc}") from exc
    finally:
        for temporary, _destination in prepared:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args(argv)
    try:
        request = _load_json(args.input, "input")
        try:
            prior_ledger = _load_json(args.ledger, "ledger")
        except FileNotFoundError:
            prior_ledger = None
        result, next_ledger = simulate(request, prior_ledger)
        _replace_pair(args.output, _encoded_json(result), args.ledger, _encoded_json(next_ledger))
    except (InputError, OSError, ValueError) as exc:
        print("INPUT_ERROR: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
