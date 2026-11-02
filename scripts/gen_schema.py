"""Generate ``schemas/fixture.schema.json`` from the pydantic fixture model.

    python scripts/gen_schema.py          # write the schema
    python scripts/gen_schema.py --check  # exit 1 if the committed schema is stale (CI)

The JSON Schema lets non-Python tools validate Traceon fixtures. The pydantic
model in ``traceon.core.schema`` is the source of truth; never edit the JSON by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from traceon.core.schema import SCHEMA_VERSION, Fixture  # noqa: E402

OUT = ROOT / "schemas" / "fixture.schema.json"
SCHEMA_ID = "https://github.com/YourAnonELF/traceon-replay/blob/main/schemas/fixture.schema.json"


def render() -> str:
    schema = Fixture.model_json_schema(mode="validation")
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": f"Traceon fixture ({SCHEMA_VERSION})",
        **{k: v for k, v in schema.items() if k != "title"},
    }
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed schema is out of date"
    )
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(
                f"{OUT.relative_to(ROOT).as_posix()} is out of date; run python scripts/gen_schema.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.relative_to(ROOT).as_posix()} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
