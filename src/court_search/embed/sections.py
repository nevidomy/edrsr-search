from __future__ import annotations

from typing import Any

from court_search.domain.enums import SOURCE_REPO_BY_JURISDICTION, Jurisdiction

# Official EDRSR snapshot sections. GPU jobs should run one of these at a time.
SECTIONS: list[dict[str, Any]] = [
    {
        "id": "civil",
        "jurisdiction": Jurisdiction.CIVIL,
        "folder": "edrsr-tspk",
        "repo": SOURCE_REPO_BY_JURISDICTION[Jurisdiction.CIVIL],
        "rows": 38_228_166,
        "note": "civil procedure; 67.7% судові накази",
    },
    {
        "id": "administrative",
        "jurisdiction": Jurisdiction.ADMINISTRATIVE,
        "folder": "edrsr-kas",
        "repo": SOURCE_REPO_BY_JURISDICTION[Jurisdiction.ADMINISTRATIVE],
        "rows": 21_743_836,
        "note": "administrative cassation",
    },
    {
        "id": "criminal",
        "jurisdiction": Jurisdiction.CRIMINAL,
        "folder": "edrsr-kpk",
        "repo": SOURCE_REPO_BY_JURISDICTION[Jurisdiction.CRIMINAL],
        "rows": 16_712_666,
        "note": "criminal; includes ~1.0M вироки",
    },
    {
        "id": "commercial",
        "jurisdiction": Jurisdiction.COMMERCIAL,
        "folder": "edrsr-gpk",
        "repo": SOURCE_REPO_BY_JURISDICTION[Jurisdiction.COMMERCIAL],
        "rows": 7_265_154,
        "note": "commercial procedure",
    },
    {
        "id": "administrative_offense",
        "jurisdiction": Jurisdiction.ADMINISTRATIVE_OFFENSE,
        "folder": "edrsr-kupap",
        "repo": SOURCE_REPO_BY_JURISDICTION[Jurisdiction.ADMINISTRATIVE_OFFENSE],
        "rows": 11_829_105,
        "note": "KUpAP; 99.6% рішення",
    },
]

SECTION_BY_ID = {item["id"]: item for item in SECTIONS}


def section_ids() -> list[str]:
    return [item["id"] for item in SECTIONS]
