"""Ask the GraphQL schema what the leads query looks like.

This replaces the part of discovery that a browser could not do. Rather than
capturing a request and reverse-engineering it, GraphQL will simply describe
itself: which root queries exist, what arguments they take, and what fields
their result type has.

Two things make this worth having as a command rather than a one-off. It reads
only the schema — field *names*, never field *values* — so it can be run
against production without a single customer record leaving the portal. And
when the application is redeployed with a renamed field, rerunning it produces
the corrected query in one step instead of an afternoon of guessing.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

ROOT_FIELDS_QUERY = """
query IntrospectRoot {
  __schema {
    queryType {
      fields {
        name
        args { name type { name kind ofType { name kind } } }
        type { name kind ofType { name kind ofType { name kind } } }
      }
    }
  }
}
""".strip()

TYPE_FIELDS_QUERY = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
""".strip()

# Field names not worth putting in a monitoring query.
_NOISE = {"__typename"}


def _unwrap(type_ref: dict[str, Any] | None) -> tuple[str, bool]:
    """Reduce a nested GraphQL type reference to (name, is_list)."""
    is_list = False
    current = type_ref or {}
    for _ in range(6):
        if current.get("kind") == "LIST":
            is_list = True
        name = current.get("name")
        if name:
            return name, is_list
        current = current.get("ofType") or {}
    return "Unknown", is_list


def find_leads_query(root_fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the root query most likely to return the leads collection."""
    scored: list[tuple[int, dict[str, Any]]] = []

    for field in root_fields:
        name = str(field.get("name", ""))
        lowered = name.lower()
        score = 0

        if lowered == "leads":
            score += 100
        elif "lead" in lowered:
            score += 50

        _, is_list = _unwrap(field.get("type"))
        if is_list:
            score += 20

        if score:
            scored.append((score, field))

    if not scored:
        return None

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def build_query(operation: str, field_names: list[str], args: list[dict[str, Any]]) -> str:
    """Render a GraphQL document selecting the given fields."""
    usable = [n for n in field_names if n not in _NOISE]
    indented = "\n".join(f"    {name}" for name in usable)

    if args:
        declarations = ", ".join(f"${arg['name']}: {_unwrap(arg.get('type'))[0]}" for arg in args)
        passed = ", ".join(f"{arg['name']}: ${arg['name']}" for arg in args)
        header = f"query Leads({declarations}) {{\n  {operation}({passed}) {{"
    else:
        header = f"query Leads {{\n  {operation} {{"

    return f"{header}\n{indented}\n  }}\n}}"


def run_introspection(settings: Settings) -> str:
    """Introspect the endpoint and return a human-readable report."""
    from .clients.graphql import GraphQLLeadsClient

    lines: list[str] = ["# GraphQL introspection", ""]

    with GraphQLLeadsClient(settings) as client:
        root = client.execute(ROOT_FIELDS_QUERY, operation_name="IntrospectRoot")
        fields = (((root or {}).get("__schema") or {}).get("queryType") or {}).get("fields") or []

        lines.append(f"The schema exposes {len(fields)} root queries.")
        lines.append("")

        leads_field = find_leads_query(fields)
        if leads_field is None:
            lines.extend(
                [
                    "**No lead-like query found.** Available root queries:",
                    "",
                    "```",
                    "\n".join(sorted(str(f.get("name")) for f in fields)),
                    "```",
                    "",
                    "Pick the right one and set LEADS_GRAPHQL_QUERY by hand.",
                ]
            )
            return "\n".join(lines)

        operation = str(leads_field["name"])
        type_name, is_list = _unwrap(leads_field.get("type"))
        args = leads_field.get("args") or []

        lines.extend(
            [
                f"Leads query: `{operation}`",
                f"Returns: {'a list of ' if is_list else ''}`{type_name}`",
                "",
            ]
        )

        if args:
            lines.append("Arguments:")
            lines.append("")
            for arg in args:
                arg_type, _ = _unwrap(arg.get("type"))
                lines.append(f"- `{arg['name']}`: {arg_type}")
            lines.append("")

        type_data = client.execute(
            TYPE_FIELDS_QUERY, {"name": type_name}, operation_name="IntrospectType"
        )
        type_fields = ((type_data or {}).get("__type") or {}).get("fields") or []
        names = [str(f.get("name")) for f in type_fields]

        lines.extend(
            [
                f"`{type_name}` has {len(names)} fields:",
                "",
                "```",
                ", ".join(sorted(n for n in names if n not in _NOISE)),
                "```",
                "",
                "## Configuration",
                "",
                "Put this in LEADS_GRAPHQL_QUERY (as a single-line value with \\n, or",
                "in .env using quotes):",
                "",
                "```graphql",
                build_query(operation, names, args),
                "```",
            ]
        )

    return "\n".join(lines)
