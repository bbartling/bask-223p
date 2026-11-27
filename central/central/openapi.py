"""OpenAPI 3.1 document for the collector."""
from __future__ import annotations


def build_openapi(*, sparql: bool) -> dict:
    paths = {
        "/api/edges": {
            "get": {
                "operationId": "listEdges",
                "summary": "Edges and the last stored export",
                "responses": {"200": {"description": "Edge status. No tokens."}, "401": {"description": "Unauthorized"}},
            }
        },
        "/api/model.ttl": _model("turtle", "Union of every named graph, Turtle"),
        "/api/model.trig": _model("trig", "Dataset: default graph is the union, plus one named graph per edge"),
        "/api/model.jsonld": _model("jsonld", "JSON-LD named graph per edge"),
        "/api/model": _model("negotiate", "Union or named graphs, selected with Accept"),
        "/api/edges/{site}/{edge}/model.ttl": {
            "get": {
                "operationId": "getEdgeModel",
                "summary": "Latest Turtle stored for one edge",
                "parameters": [_path("site"), _path("edge")],
                "responses": {
                    "200": {"description": "text/turtle"},
                    "401": {"description": "Unauthorized"},
                    "404": {"description": "No export"},
                },
            }
        },
        "/api/reconcile": {
            "get": {
                "operationId": "getReconcile",
                "summary": "Reconcile report for edges that share a bacnet:// ref",
                "responses": {"200": {"description": "JSON report"}, "401": {"description": "Unauthorized"}},
            }
        },
        "/api/validation": {
            "get": {
                "operationId": "getValidation",
                "summary": "223P reference check and, when configured, official SHACL",
                "responses": {"200": {"description": "JSON report"}, "401": {"description": "Unauthorized"}},
            }
        },
        "/api/pull": {
            "post": {
                "operationId": "pullAll",
                "summary": "Pull every edge now",
                "responses": {"200": {"description": "Per-edge result"}, "401": {"description": "Unauthorized"}},
            }
        },
        "/api/pull/{site}/{edge}": {
            "post": {
                "operationId": "pullOne",
                "summary": "Pull one edge now",
                "parameters": [_path("site"), _path("edge")],
                "responses": {
                    "200": {"description": "Pull result"},
                    "401": {"description": "Unauthorized"},
                    "404": {"description": "Unknown edge"},
                    "502": {"description": "The edge did not return a zip"},
                },
            }
        },
        "/api/edges/{site}/{edge}/export": {
            "post": {
                "operationId": "pushExport",
                "summary": "Authenticated push of one edge export",
                "parameters": [
                    _path("site"),
                    _path("edge"),
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/zip": {"schema": {"type": "string", "format": "binary"}},
                        "text/turtle": {"schema": {"type": "string"}},
                    },
                },
                "responses": {
                    "201": {"description": "Stored a new snapshot"},
                    "200": {"description": "Same model bytes as the latest snapshot"},
                    "401": {"description": "Unauthorized"},
                    "404": {"description": "Edge is not in the config"},
                    "409": {"description": "Idempotency-Key reused with a different body"},
                    "413": {"description": "Body larger than max_upload_bytes"},
                },
            }
        },
        "/api/openapi.json": {
            "get": {
                "operationId": "getOpenApi",
                "summary": "This document",
                "responses": {"200": {"description": "OpenAPI JSON"}, "401": {"description": "Unauthorized"}},
            }
        },
    }
    sparql_path = {
        "post": {
            "operationId": "sparql",
            "summary": "Read-only SPARQL. Present in this document only when sparql is true",
            "responses": {
                "200": {"description": "SELECT or ASK JSON"},
                "400": {"description": "Update or bad query"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "SPARQL is off"},
            },
        }
    }
    if sparql:
        paths["/api/sparql"] = sparql_path
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "open-hvac-edge central collector",
            "version": "0.1.0",
            "description": (
                "Reads 223P exports from configured edges. It does not connect to controllers "
                "or stations. SPARQL is omitted unless sparql is true."
            ),
        },
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}
        },
        "security": [{"bearer": []}],
        "paths": paths,
    }


def _path(name: str) -> dict:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


def _model(kind: str, summary: str) -> dict:
    return {
        "get": {
            "operationId": f"getModel{kind.title()}",
            "summary": summary,
            "responses": {
                "200": {"description": "Model"},
                "401": {"description": "Unauthorized"},
                "404": {"description": "No exports stored"},
            },
        }
    }
