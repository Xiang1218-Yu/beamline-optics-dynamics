import argparse
import json
import sys
from .core import InputError, simulate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as handle:
            request = json.load(handle)
        try:
            with open(args.ledger, encoding="utf-8") as handle:
                ledger = json.load(handle)
        except FileNotFoundError:
            ledger = None
        result, next_ledger = simulate(request, ledger)
        raise NotImplementedError("atomic persistence has not been implemented")
    except (InputError, ValueError, OSError, json.JSONDecodeError, NotImplementedError) as exc:
        print("INPUT_ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
