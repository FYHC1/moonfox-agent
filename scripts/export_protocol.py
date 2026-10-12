"""Export protocol schemas/fixtures; run with an installed moonfox-app distribution."""

import argparse
import json
from pathlib import Path

from moonfox_app.server.export import protocol_document, protocol_fixtures

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "packages" / "web-ui" / "src" / "generated"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()
    documents = {"protocol.json": protocol_document(), "fixtures.json": protocol_fixtures()}
    stale: list[str] = []
    for name, document in documents.items():
        text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        expected = text.encode("utf-8")
        path = GENERATED / name
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                stale.append(str(path.relative_to(ROOT)))
        else:
            GENERATED.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    if stale:
        print("Stale protocol exports: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
