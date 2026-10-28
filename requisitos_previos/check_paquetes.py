"""Compara los paquetes instalados con requirements.txt (sale con 1 si falta alguno)."""

import re
import sys
from importlib.metadata import PackageNotFoundError, version


def _as_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def main(requirements: str) -> int:
    missing = 0
    with open(requirements, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:>=\s*([\d.]+))?", line)
            if not match:
                continue
            name, min_version = match.groups()
            try:
                installed = version(name)
            except PackageNotFoundError:
                print(f"FALTA {name} (no instalado)")
                missing += 1
                continue
            if min_version and _as_tuple(installed) < _as_tuple(min_version):
                print(f"FALTA {name} {installed} (se requiere >= {min_version})")
                missing += 1
            else:
                print(f"OK {name} {installed}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
