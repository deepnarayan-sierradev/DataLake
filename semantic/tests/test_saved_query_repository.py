"""Tests for the saved query repository (FR-3.4). DynamoDB mocked with moto."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from semantic.saved_query import SavedQuery
from semantic.saved_query_repository import SavedQueryNotFoundError, SavedQueryRepository

_REGION = "us-east-1"


def _create_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName="EdlSavedQuery",
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "query_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "query_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _query(query_id: str = "revenue-by-industry") -> SavedQuery:
    return SavedQuery(
        query_id=query_id,
        name="Revenue by industry",
        entity="company",
        metrics=("total_revenue",),
        dimensions=("industry",),
        created_by="deep.narayan",
    )


class TestSavedQueryRepository:
    @mock_aws
    def test_save_and_get_roundtrip(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SavedQueryRepository(region_name=_REGION)
        repo.save("demo", _query())
        got = repo.get("demo", "revenue-by-industry")
        assert got.name == "Revenue by industry"
        assert got.metrics == ("total_revenue",)
        assert got.to_request().entity == "company"

    @mock_aws
    def test_get_missing_raises(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SavedQueryRepository(region_name=_REGION)
        with pytest.raises(SavedQueryNotFoundError):
            repo.get("demo", "nope")

    @mock_aws
    def test_list_scoped_to_tenant(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SavedQueryRepository(region_name=_REGION)
        repo.save("demo", _query("q1"))
        repo.save("demo", _query("q2"))
        repo.save("acme-corp", _query("q3"))
        demo_queries = repo.list_for_tenant("demo")
        assert {q.query_id for q in demo_queries} == {"q1", "q2"}

    def test_invalid_tenant_rejected(self):
        repo = SavedQueryRepository(region_name=_REGION)
        with pytest.raises(ValueError):
            repo.get("BAD_TENANT", "q1")


class TestSavedQueryModel:
    def test_invalid_query_id_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _query(query_id="Bad ID!")


class TestEveryDeclaredFieldRoundTrips:
    """
    Serialisation named five fields by hand, so a saved query gaining filters would have been
    stored without them and read back as unfiltered — a filter that silently does not apply.
    """

    @mock_aws
    def test_filters_and_time_range_survive_a_save_and_reload(self) -> None:
        from datetime import date

        from semantic.query_compiler import SemanticFilter, TimeRangeFilter
        from semantic.semantic_model import TimeGrain

        _create_table(boto3.resource("dynamodb", region_name=_REGION))
        repo = SavedQueryRepository(region_name=_REGION)
        stored = SavedQuery(
            query_id="q-filtered",
            name="Filtered",
            entity="company",
            metrics=("total_revenue",),
            created_by="ops@example.test",
            filters=(SemanticFilter(dimension="industry", operator="eq", value="Tech"),),
            joined_dimensions=(("franchisee", "franchisee_name"),),
            time_dimension="booked_date",
            time_grain=TimeGrain.QUARTER,
            time_range=TimeRangeFilter(
                time_dimension="booked_date", start=date(2026, 1, 1), end=date(2026, 3, 31)
            ),
            row_limit=250,
        )
        repo.save("demo", stored)

        reloaded = repo.get("demo", "q-filtered")
        assert reloaded.filters == stored.filters
        assert reloaded.joined_dimensions == stored.joined_dimensions
        assert reloaded.time_grain is TimeGrain.QUARTER
        assert reloaded.time_range == stored.time_range
        assert reloaded.row_limit == 250
        # And the request the compiler receives carries them too.
        assert reloaded.to_request().filters == stored.filters

    @mock_aws
    def test_a_listing_returns_the_filters_as_well(self) -> None:
        from semantic.query_compiler import SemanticFilter

        _create_table(boto3.resource("dynamodb", region_name=_REGION))
        repo = SavedQueryRepository(region_name=_REGION)
        repo.save(
            "demo",
            SavedQuery(
                query_id="q-listed",
                name="Listed",
                entity="company",
                metrics=("total_revenue",),
                created_by="ops@example.test",
                filters=(SemanticFilter(dimension="industry", operator="in", values=("A", "B")),),
            ),
        )
        listed = repo.list_for_tenant("demo")
        assert len(listed) == 1
        assert listed[0].filters[0].values == ("A", "B")
