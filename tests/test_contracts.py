import pytest
from pydantic import ValidationError

from court_search.domain.contracts import SearchRequest


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "ухвала", "invented_court": "Kyiv"})


def test_query_bounds() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": ""})
    SearchRequest.model_validate({"query": "ухвала"})
