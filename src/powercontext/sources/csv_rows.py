# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Row-level CSV Sources that keep original headers, values, and identity.

Every data record of one offline CSV document becomes its own ``CAPTURED``
Source identified by the document and the record's primary key value, so a
retrieval citation can always be traced back to the complete original row.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_TYPE = "csv-row"
CSV_ROW_DEFINITION_VERSION = "1"
SOURCE_ID_PREFIX = "csv"
SOURCE_ID_SEPARATOR = ":"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document addressed by document and primary key."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed data record in original header order."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A single captured CSV record with a stable, delimiter-safe identity."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    definition_version: str = CSV_ROW_DEFINITION_VERSION
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable identity ``csv:<document>:<key>`` for one record."""

    _require_document_id(document_id)
    _require_primary_key_value(primary_key_value)
    identity = (
        f"{SOURCE_ID_PREFIX}{SOURCE_ID_SEPARATOR}{quote(document_id, safe='')}"
        f"{SOURCE_ID_SEPARATOR}{quote(primary_key_value, safe='')}"
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"csv row source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate one CSV document atomically into row inputs."""

    document_id = value.document_id
    _require_document_id(document_id)
    primary_key = value.primary_key
    if not isinstance(primary_key, str) or not primary_key.strip():
        raise ValueError("csv primary_key must be a non-blank string")  # noqa: TRY003

    text = _decode_document(value.content)
    records = _read_records(text)
    if not records:
        raise ValueError("csv document must contain a header record")  # noqa: TRY003

    header = records[0]
    _require_header(header)
    if primary_key not in header:
        raise ValueError(f"csv primary_key {primary_key!r} does not exactly match a header column")  # noqa: TRY003
    key_index = header.index(primary_key)
    columns = tuple(header)

    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError(f"csv record must have {len(columns)} fields, found {len(record)}")  # noqa: TRY003
        primary_key_value = record[key_index]
        _require_primary_key_value(primary_key_value)
        if primary_key_value in seen:
            raise ValueError(f"csv primary_key value {primary_key_value!r} is duplicated in this document")  # noqa: TRY003
        seen.add(primary_key_value)
        # Fail before returning any row when an identity cannot be built.
        csv_row_source_id(document_id, primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read the row-level CSV Source described by one Definition."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        _require_row(value)
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV record as canonical standard text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = row_mapping(source.columns, source.values)
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_TYPE,
            source_id=source.source_id,
            content=serialize_row(row),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


def row_mapping(columns: tuple[str, ...], values: tuple[str, ...]) -> dict[str, JsonValue]:
    """Project one record onto its header while keeping column order."""

    if len(columns) != len(values):
        raise ValueError("csv columns and values must have the same width")  # noqa: TRY003
    return dict(zip(columns, values, strict=True))


def serialize_row(row: dict[str, JsonValue]) -> str:
    """Serialize a record with the canonical compact JSON representation."""

    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_DEFINITION_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _decode_document(content: bytes) -> str:
    try:
        return bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("csv content must be valid strict UTF-8") from error  # noqa: TRY003


def _read_records(text: str) -> list[list[str]]:
    if not text:
        raise ValueError("csv document must not be empty")  # noqa: TRY003
    try:
        return list(
            csv.reader(
                io.StringIO(text, newline=""),
                delimiter=",",
                quotechar='"',
                doublequote=True,
                strict=True,
            )
        )
    except csv.Error as error:
        raise ValueError(f"csv document is not well formed: {error}") from error  # noqa: TRY003


def _require_header(header: list[str]) -> None:
    if not header:
        raise ValueError("csv header must not be empty")  # noqa: TRY003
    for column in header:
        if not column.strip():
            raise ValueError("csv header columns must not be blank")  # noqa: TRY003
    if len(set(header)) != len(header):
        raise ValueError("csv header columns must be unique")  # noqa: TRY003


def _require_document_id(document_id: object) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("csv document_id must be a non-blank string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("csv document_id must not contain leading or trailing whitespace")  # noqa: TRY003


def _require_primary_key_value(primary_key_value: object) -> None:
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("csv primary_key_value must be a non-blank string")  # noqa: TRY003


def _require_row(value: CSVRowInput) -> None:
    _require_document_id(value.document_id)
    if not isinstance(value.primary_key, str) or not value.primary_key.strip():
        raise ValueError("csv primary_key must be a non-blank string")  # noqa: TRY003
    _require_primary_key_value(value.primary_key_value)
    if not value.columns:
        raise ValueError("csv columns must not be empty")  # noqa: TRY003
    if len(value.columns) != len(value.values):
        raise ValueError("csv columns and values must have the same width")  # noqa: TRY003
    for column in value.columns:
        if not column.strip():
            raise ValueError("csv header columns must not be blank")  # noqa: TRY003
    if len(set(value.columns)) != len(value.columns):
        raise ValueError("csv header columns must be unique")  # noqa: TRY003
    if value.primary_key not in value.columns:
        raise ValueError(f"csv primary_key {value.primary_key!r} does not exactly match a column")  # noqa: TRY003


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_TYPE",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
    "row_mapping",
    "serialize_row",
]
