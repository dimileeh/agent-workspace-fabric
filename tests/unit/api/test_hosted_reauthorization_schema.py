"""Optional reauthorization is omitted, not nullable, in every request schema."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from awf.api.app import create_app
from awf.api.schemas_responses import WorkspaceGuideRequest, WorkspaceRemonitorRequest
from tests.unit.test_hosted_runtime_contract import hosted_create


def pair():
    expected = hosted_create()["hosted"]
    replacement = deepcopy(expected)
    for role, binding in replacement["credentials"].items():
        for key in ("preparation_id", "grant_id", "idempotency_key"):
            binding[key] = f"replacement-{role}-{key}"
    return {"expected_association": expected, "replacement_association": replacement}


@pytest.mark.parametrize("model", [WorkspaceGuideRequest, WorkspaceRemonitorRequest])
@pytest.mark.parametrize("document", ["standalone", "openapi"])
def test_reauthorization_schema_matches_omitted_object_and_null_contract(model, document):
    body = {"directive": "Continue review"} if model is WorkspaceGuideRequest else {}
    if document == "standalone":
        schema = model.model_json_schema()
        request_schema = schema
    else:
        spec = create_app(use_lifespan=False).openapi()
        action = "guide" if model is WorkspaceGuideRequest else "remonitor"
        request_body = spec["paths"][f"/v1/workspaces/{{workspace_id}}/{action}"]["post"][
            "requestBody"
        ]
        assert request_body["required"] is True
        schema = {
            **request_body["content"]["application/json"]["schema"],
            "components": spec["components"],
        }
        request_schema = spec["components"]["schemas"][model.__name__]
    validator = Draft202012Validator(schema)
    assert "hosted_reauthorization" not in request_schema.get("required", [])
    assert validator.is_valid(body)
    assert model.model_validate(body).hosted_reauthorization is None
    supplied = {**body, "hosted_reauthorization": pair()}
    assert validator.is_valid(supplied)
    assert model.model_validate(supplied).hosted_reauthorization is not None
    null = {**body, "hosted_reauthorization": None}
    with pytest.raises(ValidationError, match="HOSTED_REAUTHORIZATION_REQUIRED"):
        model.model_validate(null)
    assert not validator.is_valid(null)
    assert "default" not in request_schema["properties"]["hosted_reauthorization"]
    malformed = {**body, "hosted_reauthorization": {**pair(), "allow": True}}
    assert not validator.is_valid(malformed)
    with pytest.raises(ValidationError):
        model.model_validate(malformed)
