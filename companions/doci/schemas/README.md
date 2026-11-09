# DOCI JSON Schemas

Contract definitions for the REST API payloads. Each schema is a
draft 2020-12 JSON Schema describing the shape of a single
operation's request or response. Use them to validate inputs in
client code, generate types in any language, or wire up an OpenAPI
spec on top.

| Schema | Subject |
|---|---|
| [`document.schema.json`](document.schema.json) | The `documents` row plus the wire shape returned by `GET /api/documents.php` |
| [`thread.schema.json`](thread.schema.json) | Request body for `POST /api/thread.php` (anchored discussion) |
| [`inbox.schema.json`](inbox.schema.json) | Request body for `POST /api/inbox.php?action=add` |
| [`version.schema.json`](version.schema.json) | A single entry from `GET /api/versions.php?document_guid=…` |

These schemas describe the **wire** contract, not the database. For
the storage-side dictionary (Postgres column types, constraints, the
`get_document_hierarchy` function), see
[`../docs/data-dictionary.md`](../docs/data-dictionary.md). For the
endpoints themselves, see
[`../docs/05-api-reference.md`](../docs/05-api-reference.md).

## Validating with `ajv` (Node)

```bash
npm i -g ajv-cli
ajv -s schemas/document.schema.json -d my-payload.json
```

## Validating with `jsonschema` (Python)

```python
import json, jsonschema
schema = json.load(open("schemas/document.schema.json"))
payload = json.load(open("my-payload.json"))
jsonschema.validate(payload, schema)
```

## Stability

Schema files follow the same semver as DOCI itself
([`../RELEASING.md`](../RELEASING.md)). Breaking field changes — a
required field added, a type widened, a value enum narrowed — only
ship in a new major version.
