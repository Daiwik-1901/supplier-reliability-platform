"""Sequential, formatted identifier registry.

The year segment is the fixed literal 2026 (dataset edition), not the record date.
Extra kinds beyond the Global Constraints: CPR- (contract_price), REQ- (compliance_requirement),
CERT- (certification), CAP- (capacity_declaration), all six-digit counters.
"""

_FORMATS: dict[str, str] = {
    "supplier": "SUP-{n:06d}",
    "contract": "CTR-{n:06d}",
    "po": "PO-2026-{n:06d}",
    "po_line": "POL-2026-{n:06d}",
    "shipment": "SHP-2026-{n:06d}",
    "shipment_line": "SHL-2026-{n:06d}",
    "event": "EVT-2026-{n:06d}",
    "inspection": "INS-2026-{n:06d}",
    "invoice": "INV-2026-{n:06d}",
    "risk": "RSK-2026-{n:06d}",
    "score": "SCORE-2026-{n:06d}",
    "raw": "RAW-{n:06d}",
    "exception": "EXC-{n:06d}",
    "case": "CASE-{n:06d}",
    "contract_price": "CPR-{n:06d}",
    "compliance_requirement": "REQ-{n:06d}",
    "certification": "CERT-{n:06d}",
    "capacity_declaration": "CAP-{n:06d}",
}
_SITE = "SITE-{parent}-{n:02d}"


class IdRegistry:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, str | None], int] = {}
        self._ids: dict[str, list[str]] = {}
        self._seen: dict[str, set[str]] = {}

    def next(self, kind: str, parent: str | None = None) -> str:
        if kind == "site":
            if parent is None:
                raise ValueError("site ids require a parent supplier id")
        elif kind not in _FORMATS:
            raise ValueError(f"unknown id kind {kind!r}")
        key = (kind, parent if kind == "site" else None)
        n = self._counters.get(key, 0) + 1
        self._counters[key] = n
        new_id = _SITE.format(parent=parent, n=n) if kind == "site" else _FORMATS[kind].format(n=n)
        self.register(kind, new_id)
        return new_id

    def register(self, kind: str, id_: str) -> None:
        seen = self._seen.setdefault(kind, set())
        if id_ in seen:
            raise ValueError(f"duplicate {kind} id {id_}")
        seen.add(id_)
        self._ids.setdefault(kind, []).append(id_)

    def exists(self, kind: str, id_: str) -> bool:
        return id_ in self._seen.get(kind, ())

    def all(self, kind: str) -> list[str]:
        return list(self._ids.get(kind, []))
