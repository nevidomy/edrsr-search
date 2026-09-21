from __future__ import annotations

from enum import StrEnum


class Jurisdiction(StrEnum):
    CIVIL = "civil"
    ADMINISTRATIVE = "administrative"
    CRIMINAL = "criminal"
    COMMERCIAL = "commercial"
    ADMINISTRATIVE_OFFENSE = "administrative_offense"


class SearchMode(StrEnum):
    HYBRID = "hybrid"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class FilterField(StrEnum):
    COURT = "court"
    JURISDICTION = "jurisdiction"
    JUDGMENT_TYPE = "judgment_type"
    CATEGORY = "category"


# Source justice_kind codes from the HF schema / EDRSR justice_kinds dictionary.
JURISDICTION_BY_KIND: dict[int, Jurisdiction] = {
    1: Jurisdiction.CIVIL,
    2: Jurisdiction.CRIMINAL,
    3: Jurisdiction.COMMERCIAL,
    4: Jurisdiction.ADMINISTRATIVE,
    5: Jurisdiction.ADMINISTRATIVE_OFFENSE,
}

SOURCE_REPO_BY_JURISDICTION: dict[Jurisdiction, str] = {
    Jurisdiction.CIVIL: "overthelex/edrsr-tspk",
    Jurisdiction.ADMINISTRATIVE: "overthelex/edrsr-kas",
    Jurisdiction.CRIMINAL: "overthelex/edrsr-kpk",
    Jurisdiction.COMMERCIAL: "overthelex/edrsr-gpk",
    Jurisdiction.ADMINISTRATIVE_OFFENSE: "overthelex/edrsr-kupap",
}

REGISTRY_REVIEW_URL = "https://reyestr.court.gov.ua/Review/{doc_id}"
