from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from court_search.domain.enums import FilterField, Jurisdiction


class DictionarySet:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.jurisdictions = _load(root / "jurisdictions.yaml")
        self.judgment_types = _load(root / "judgment_types.yaml")
        self.courts = _load(root / "courts.yaml")
        self.categories = _load(root / "categories.yaml")

    def label(self, field: FilterField, code: str | None) -> str | None:
        if not code:
            return None
        return self._table(field).get("labels", {}).get(str(code))

    def version(self, field: FilterField) -> str | None:
        return self._table(field).get("version")

    def provenance(self, field: FilterField) -> str | None:
        return self._table(field).get("provenance")

    def values(
        self,
        field: FilterField,
        *,
        query: str | None = None,
        jurisdiction: Jurisdiction | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        table = self._table(field)
        labels: dict[str, str | None] = table.get("labels", {})
        items: list[dict[str, Any]] = []
        needle = query.casefold() if query else None
        for code, label in labels.items():
            if jurisdiction and field == FilterField.CATEGORY:
                scoped = table.get("by_jurisdiction", {}).get(jurisdiction.value, {})
                if scoped and code not in scoped:
                    continue
            hay = f"{code} {label or ''}".casefold()
            if needle and needle not in hay:
                continue
            items.append(
                {
                    "code": str(code),
                    "label": label,
                    "dictionary_version": table.get("version"),
                }
            )
            if len(items) >= limit:
                break
        return items

    def _table(self, field: FilterField) -> dict[str, Any]:
        return {
            FilterField.COURT: self.courts,
            FilterField.JURISDICTION: self.jurisdictions,
            FilterField.JUDGMENT_TYPE: self.judgment_types,
            FilterField.CATEGORY: self.categories,
        }[field]


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": None, "provenance": None, "labels": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("labels", {})
    return data
