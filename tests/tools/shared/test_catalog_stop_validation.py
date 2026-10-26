"""Stop plans omit execution choices without bypassing supplied-field validation."""
import pytest
from app.core.llm_feedback import OutputValidationError
from app.schemas.catalog_search import CatalogDiscoveryPlan, CatalogLookupPlan
from app.tools.catalog.facets_plan import FacetQueryPlan, _validate_catalog_facet_plan
from app.tools.structured_planning import output_schema, require_decisions


@pytest.mark.parametrize('schema,decision,optional', [
    (FacetQueryPlan, {'supported': True}, ('facet', 'count_scope')),
    (CatalogDiscoveryPlan, {'disposition': 'ready'}, ('candidate_unit', 'requested_fields')),
    (CatalogLookupPlan, {'disposition': 'ready'}, ('requested_fields',)),
])
def test_execution_choices_remain_required_in_program_and_prompt(schema, decision, optional):
    data = {field: [] for field in schema.model_required_fields} | decision
    prompt = output_schema(schema, schema.model_required_fields)
    assert set(prompt['allOf'][-1]['else']['required']) == set(optional)
    for field in optional:
        incomplete = {key: value for key, value in data.items() if key != field}
        with pytest.raises(OutputValidationError):
            require_decisions(incomplete, schema.model_required_fields, schema=schema)


def test_executable_spec_values_still_need_spec_key():
    with pytest.raises(OutputValidationError, match='spec_value requires spec_key'):
        _validate_catalog_facet_plan(FacetQueryPlan(facet='spec_value'))


@pytest.mark.parametrize('fields', [
    {'conditions': [{'key': 'price', 'operator': 'eq', 'values': ['-1']}]},
])
def test_stop_does_not_bypass_condition_value_validation(fields):
    plan = FacetQueryPlan(supported=False, query='指定商品版本价格', **fields)
    with pytest.raises(OutputValidationError):
        _validate_catalog_facet_plan(plan)


def test_stop_still_requires_explicit_requirement_decisions():
    with pytest.raises(OutputValidationError):
        require_decisions(
            {'supported': False, 'selection': 'object'},
            FacetQueryPlan.model_required_fields, schema=FacetQueryPlan,
        )
