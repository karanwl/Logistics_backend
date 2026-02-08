import json
from typing import Any

from planning import generate_tomorrow_plan


def main() -> int:
    data: dict[str, Any] = generate_tomorrow_plan()
    output = {
        "meta": data.get("meta", {}),
        "routes": data.get("routes", []),
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
