import logging
import os
from datahub.ingestion.graph.filters import SearchFilterRule
from tests.consistency_utils import wait_for_writes_to_sync
import tempfile
from random import randint
from tests.utilities.file_emitter import FileEmitter
from typing import Iterable, List, Optional, Union

import pytest
# import tenacity
from datahub.api.entities.dataset.dataset import Dataset
from datahub.api.entities.structuredproperties.structuredproperties import \
    StructuredProperties
from datahub.emitter.mce_builder import make_dataset_urn, make_schema_field_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    EntityTypeInfoClass, PropertyValueClass, StructuredPropertiesClass,
    StructuredPropertyDefinitionClass, StructuredPropertyValueAssignmentClass)
from datahub.specific.dataset import DatasetPatchBuilder
from datahub.utilities.urns.structured_properties_urn import \
    StructuredPropertyUrn
from datahub.utilities.urns.urn import Urn

from tests.utils import (delete_urns, delete_urns_from_file, get_gms_url,
                         get_sleep_info, ingest_file_via_rest,
                         wait_for_writes_to_sync)

logger = logging.getLogger(__name__)

start_index = randint(10, 10000)
dataset_urns = [
    make_dataset_urn("snowflake", f"table_foo_{i}")
    for i in range(start_index, start_index + 10)
]

schema_field_urns = [
    make_schema_field_urn(dataset_urn, "column_1")
    for dataset_urn in dataset_urns
]

generated_urns = [d for d in dataset_urns] + [f for f in schema_field_urns]


default_namespace = "io.acryl.privacy"

def create_logical_entity(
    entity_name: str,
) -> Iterable[MetadataChangeProposalWrapper]:
    mcp = MetadataChangeProposalWrapper(
        entityUrn="urn:li:entityType:" + entity_name,
        aspect=EntityTypeInfoClass(
            qualifiedName="io.datahubproject." + entity_name,
            displayName=entity_name,
        ),
    )
    return [mcp]


def create_test_data(filename: str):
    file_emitter = FileEmitter(filename)
    for mcps in create_logical_entity("dataset"):
        file_emitter.emit(mcps)

    file_emitter.close()
    wait_for_writes_to_sync()

sleep_sec, sleep_times = get_sleep_info()


@pytest.fixture(scope="module", autouse=False)
def graph() -> DataHubGraph:
    graph: DataHubGraph = DataHubGraph(
        config=DatahubClientConfig(server=get_gms_url())
    )
    return graph


@pytest.fixture(scope="module", autouse=False)
def ingest_cleanup_data(request):
    new_file, filename = tempfile.mkstemp()
    try:
        create_test_data(filename)
        print("ingesting structured properties test data")
        ingest_file_via_rest(filename)
        yield
        print("removing structured properties test data")
        delete_urns_from_file(filename)
        delete_urns(generated_urns)
        wait_for_writes_to_sync()
    finally:
        os.remove(filename)


@pytest.mark.dependency()
def test_healthchecks(wait_for_healthchecks):
    # Call to wait_for_healthchecks fixture will do the actual functionality.
    pass


def create_property_definition(
    property_name: str,
    graph: DataHubGraph,
    namespace: str = default_namespace,
    value_type: str = "string",
    cardinality: str = "SINGLE",
    allowed_values: Optional[List[PropertyValueClass]] = None,
    entity_types: Optional[List[str]] = None,
):
    structured_property_definition = StructuredPropertyDefinitionClass(
        qualifiedName=f"{namespace}.{property_name}",
        valueType=Urn.make_data_type_urn(value_type),
        description="The retention policy for the dataset",
        entityTypes=[Urn.make_entity_type_urn(e) for e in entity_types]
        if entity_types
        else [Urn.make_entity_type_urn("dataset")],
        cardinality=cardinality,
        allowedValues=allowed_values,
    )

    mcp = MetadataChangeProposalWrapper(
        entityUrn=f"urn:li:structuredProperty:{namespace}.{property_name}",
        aspect=structured_property_definition,
    )
    graph.emit(mcp)
    wait_for_writes_to_sync()


def attach_property_to_entity(
    urn: str,
    property_name: str,
    property_value: Union[str, float, List[str | float]],
    graph: DataHubGraph,
    namespace: str = default_namespace
):
    if isinstance(property_value, list):
        property_values: List[Union[str, float]] = property_value
    else:
        property_values = [property_value]

    mcp = MetadataChangeProposalWrapper(
        entityUrn=urn,
        aspect=StructuredPropertiesClass(
            properties=[
                StructuredPropertyValueAssignmentClass(
                    propertyUrn=f"urn:li:structuredProperty:{namespace}.{property_name}",
                    values=property_values,
                )
            ]
        ),
    )
    graph.emit_mcp(mcp)
    wait_for_writes_to_sync()


def get_property_from_entity(
    urn: str,
    property_name: str,
    graph: DataHubGraph,
):
    structured_properties: Optional[
        StructuredPropertiesClass
    ] = graph.get_aspect(urn, StructuredPropertiesClass)
    assert structured_properties is not None
    for property in structured_properties.properties:
        if (
            property.propertyUrn
            == f"urn:li:structuredProperty:{property_name}"
        ):
            return property.values
    return None


# @tenacity.retry(
#     stop=tenacity.stop_after_attempt(sleep_times),
#     wait=tenacity.wait_fixed(sleep_sec),
# )
@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_string(ingest_cleanup_data, graph):
    property_name = "retentionPolicy"

    create_property_definition(property_name, graph)
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.retentionPolicy")

    attach_property_to_entity(
        dataset_urns[0], property_name, ["30d"], graph=graph
    )

    try:
        attach_property_to_entity(
            dataset_urns[0], property_name, 200030, graph=graph
        )
        raise AssertionError(
            "Should not be able to attach a number to a string property"
        )
    except Exception as e:
        if not isinstance(e, AssertionError):
            pass
        else:
            raise e


# @tenacity.retry(
#     stop=tenacity.stop_after_attempt(sleep_times),
#     wait=tenacity.wait_fixed(sleep_sec),
# )
@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_double(ingest_cleanup_data, graph):
    property_name = "expiryTime"
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.{property_name}")
    create_property_definition(property_name, graph, value_type="number")

    attach_property_to_entity(
        dataset_urns[0], property_name, 2000034, graph=graph
    )

    try:
        attach_property_to_entity(
            dataset_urns[0], property_name, "30 days", graph=graph
        )
        raise AssertionError(
            "Should not be able to attach a string to a number property"
        )
    except Exception as e:
        if not isinstance(e, AssertionError):
            pass
        else:
            raise e

    try:
        attach_property_to_entity(
            dataset_urns[0], property_name, [2000034, 2000035], graph=graph
        )
        raise AssertionError(
            "Should not be able to attach a list to a number property"
        )
    except Exception as e:
        if not isinstance(e, AssertionError):
            pass
        else:
            raise e


# @tenacity.retry(
#     stop=tenacity.stop_after_attempt(sleep_times),
#     wait=tenacity.wait_fixed(sleep_sec),
# )
@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_double_multiple(ingest_cleanup_data, graph):
    property_name = "versions"
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.{property_name}")

    create_property_definition(
        property_name, graph, value_type="number", cardinality="MULTIPLE"
    )

    attach_property_to_entity(
        dataset_urns[0], property_name, [1.0, 2.0], graph=graph
    )


# @tenacity.retry(
#     stop=tenacity.stop_after_attempt(sleep_times),
#     wait=tenacity.wait_fixed(sleep_sec),
# )
@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_string_allowed_values(
    ingest_cleanup_data, graph
):
    property_name = "enumProperty"
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.{property_name}")

    create_property_definition(
        property_name,
        graph,
        value_type="string",
        cardinality="MULTIPLE",
        allowed_values=[
            PropertyValueClass(value="foo"),
            PropertyValueClass(value="bar"),
        ],
    )

    attach_property_to_entity(
        dataset_urns[0], property_name, ["foo", "bar"], graph=graph
    )

    try:
        attach_property_to_entity(
            dataset_urns[0], property_name, ["foo", "baz"], graph=graph
        )
        raise AssertionError(
            "Should not be able to attach a value not in allowed values"
        )
    except Exception as e:
        if "value: {string=baz} should be one of [" in str(e):
            pass
        else:
            raise e


@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_definition_evolution(
    ingest_cleanup_data, graph
):
    property_name = "enumProperty1234"

    create_property_definition(
        property_name,
        graph,
        value_type="string",
        cardinality="MULTIPLE",
        allowed_values=[
            PropertyValueClass(value="foo"),
            PropertyValueClass(value="bar"),
        ],
    )
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.{property_name}")

    try:
        create_property_definition(
            property_name,
            graph,
            value_type="string",
            cardinality="SINGLE",
            allowed_values=[
                PropertyValueClass(value="foo"),
                PropertyValueClass(value="bar"),
            ],
        )
        raise AssertionError(
            "Should not be able to change cardinality from MULTIPLE to SINGLE"
        )
    except Exception as e:
        if isinstance(e, AssertionError):
            raise e
        else:
            pass


# @tenacity.retry(
#     stop=tenacity.stop_after_attempt(sleep_times),
#     wait=tenacity.wait_fixed(sleep_sec),
# )
@pytest.mark.dependency(depends=["test_healthchecks"])
def test_structured_property_schema_field(ingest_cleanup_data, graph):
    property_name = (
        f"deprecationDate{randint(10, 10000)}"
    )

    create_property_definition(
        property_name,
        graph,
        namespace="io.datahubproject.test",
        value_type="date",
        entity_types=["schemaField"],
    )
    generated_urns.append(f"urn:li:structuredProperty:io.datahubproject.test.{property_name}")

    attach_property_to_entity(
        schema_field_urns[0], property_name, "2020-10-01", graph=graph, namespace="io.datahubproject.test"
    )

    assert (
        get_property_from_entity(
            schema_field_urns[0], f"io.datahubproject.test.{property_name}", graph=graph
        )
        == ["2020-10-01"]
    )

    try:
        attach_property_to_entity(
            schema_field_urns[0], property_name, 200030, graph=graph, namespace="io.datahubproject.test"
        )
        raise AssertionError(
            "Should not be able to attach a number to a DATE property"
        )
    except Exception as e:
        if not isinstance(e, AssertionError):
            pass
        else:
            raise e


def test_dataset_yaml_loader(ingest_cleanup_data, graph):
    StructuredProperties.create(
        "tests/structured_properties/test_structured_properties.yaml"
    )

    for dataset in Dataset.from_yaml(
        "tests/structured_properties/test_dataset.yaml"
    ):
        for mcp in dataset.generate_mcp():
            graph.emit(mcp)
        wait_for_writes_to_sync()

    property_name = "io.acryl.dataManagement.deprecationDate"
    assert (
        get_property_from_entity(
            make_schema_field_urn(
                make_dataset_urn("hive", "user.clicks"), "ip"
            ),
            property_name,
            graph=graph,
        )
        == ["2023-01-01"]
    )

    dataset = Dataset.from_datahub(
        graph=graph,
        urn="urn:li:dataset:(urn:li:dataPlatform:hive,user.clicks,PROD)",
    )
    field_name = "ip"
    matching_fields = [
        f
        for f in dataset.schema_metadata.fields
        if Dataset._simplify_field_path(f.id) == field_name
    ]
    assert len(matching_fields) == 1
    assert (
        matching_fields[0].structured_properties[
            Urn.make_structured_property_urn(
                "io.acryl.dataManagement.deprecationDate"
            )
        ]
        == ["2023-01-01"]
    )


def test_dataset_structured_property_validation(
    ingest_cleanup_data, graph, caplog
):
    from datahub.api.entities.dataset.dataset import Dataset

    property_name = "replicationSLA"
    property_value = 30
    value_type = "number"

    create_property_definition(
        property_name=property_name, graph=graph, value_type=value_type
    )
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.replicationSLA")

    attach_property_to_entity(
        dataset_urns[0], property_name, [property_value], graph=graph
    )

    assert Dataset.validate_structured_property(
        f"{default_namespace}.{property_name}", property_value
    ) == (
        f"{default_namespace}.{property_name}",
        float(property_value),
    )

    assert (
            Dataset.validate_structured_property("testName", "testValue") is None
    )

    bad_property_value = "2023-09-20"
    assert (
        Dataset.validate_structured_property(
            property_name, bad_property_value
        )
        is None
    )

    
def test_structured_property_search(ingest_cleanup_data, graph: DataHubGraph, caplog):

    def to_es_name(property_name, namespace=default_namespace):
        namespace_field = namespace.replace(".", "_")
        return f"structuredProperties.{namespace_field}_{property_name}"

    # Attach structured property to entity and to field
    field_property_name = f"deprecationDate{randint(10, 10000)}"

    create_property_definition(
        namespace="io.datahubproject.test",
        property_name=field_property_name,
        graph=graph, value_type="date", entity_types=["schemaField"]
    )
    generated_urns.append(f"urn:li:structuredProperty:io.datahubproject.test.{field_property_name}")

    attach_property_to_entity(
        schema_field_urns[0], field_property_name, "2020-10-01", graph=graph, namespace="io.datahubproject.test"
    )
    dataset_property_name = "replicationSLA"
    property_value = 30
    value_type = "number"

    create_property_definition(property_name=dataset_property_name, graph=graph, value_type=value_type)
    generated_urns.append(f"urn:li:structuredProperty:{default_namespace}.{dataset_property_name}")

    attach_property_to_entity(dataset_urns[0], dataset_property_name, [property_value], graph=graph)

    # [] = default entities which includes datasets, does not include fields
    entity_urns = list(graph.get_urns_by_filter(extraFilters=[
        {
            "field": to_es_name(dataset_property_name),
            "negated": "false",
            "condition": "EXISTS",
        }
    ]))
    assert len(entity_urns) == 1
    assert entity_urns[0] == dataset_urns[0]

    # Search over schema field specifically
    field_structured_prop = graph.get_aspect(entity_urn=schema_field_urns[0], aspect_type=StructuredPropertiesClass)
    assert field_structured_prop == StructuredPropertiesClass(
        properties=[
            StructuredPropertyValueAssignmentClass(
                propertyUrn=f"urn:li:structuredProperty:io.datahubproject.test.{field_property_name}",
                values=["2020-10-01"]
            )
        ]
    )

    # Search over entities that do not include the field
    field_urns = list(graph.get_urns_by_filter(entity_types=["tag"],
                                               extraFilters=[
                                                   {
                                                       "field": to_es_name(field_property_name,
                                                                           namespace="io.datahubproject.test"),
                                                       "negated": "false",
                                                       "condition": "EXISTS",
                                                   }
                                               ]))
    assert len(field_urns) == 0

    # OR the two properties together to return both results
    field_urns = list(graph.get_urns_by_filter(entity_types=["dataset", "tag"],
                                               extraFilters=[
                                                   {
                                                       "field": to_es_name(dataset_property_name),
                                                       "negated": "false",
                                                       "condition": "EXISTS",
                                                   }
                                               ]))
    assert len(field_urns) == 1
    assert dataset_urns[0] in field_urns


def test_dataset_structured_property_patch(
    ingest_cleanup_data, graph, caplog
):
    property_name = "replicationSLA"
    property_value = 30
    value_type = "number"

    create_property_definition(
        property_name=property_name,
        graph=graph,
        value_type=value_type
    )

    dataset_patcher: DatasetPatchBuilder = DatasetPatchBuilder(
        urn=dataset_urns[0]
    )

    dataset_patcher.set_structured_property(StructuredPropertyUrn.make_structured_property_urn(
        f"{default_namespace}.{property_name}"), property_value)

    for mcp in dataset_patcher.build():
        graph.emit(mcp)
    wait_for_writes_to_sync()

    dataset = Dataset.from_datahub(graph=graph, urn=dataset_urns[0])
    assert dataset.structured_properties is not None
    assert (
        [int(float(k)) for k in dataset.structured_properties[
            StructuredPropertyUrn.make_structured_property_urn(
                f"{default_namespace}.{property_name}"
            )
        ]]
        == [property_value]
    )
