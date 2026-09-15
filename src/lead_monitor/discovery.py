"""Find out how the Leads page actually gets its data.

This is the step that decides the architecture, so it is committed as code
rather than performed once by hand in DevTools. When the portal is redeployed
and the shape changes, rerunning ``lead-monitor discover`` gives a fresh answer
in a minute instead of an afternoon of guessing.

It drives a browser to the Leads page, records every XHR and fetch the page
makes, and ranks them by how much they look like the leads list: JSON content
type, an array of objects in the response, and lead-ish field names. The report
it writes is what fills in ``LEADS_API_PATH`` and the field mapping.

Nothing here runs in production. It is a development tool, and it deliberately
writes its output to a gitignored directory because captured responses contain
real personal data.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

# Field names that suggest a payload is about leads rather than, say, the
# navigation menu or a feature-flag blob.
_LEAD_FIELD_HINTS = (
    "lead",
    "name",
    "nombre",
    "email",
    "correo",
    "phone",
    "telefono",
    "tel",
    "status",
    "estado",
    "club",
    "centro",
    "created",
    "fecha",
    "alta",
)

_STATUS_HINTS = ("pre order", "preorder", "pre-order", "pedido previo")

# Responses larger than this are almost certainly bundles or images, not data.
_MAX_BODY_BYTES = 2_000_000


@dataclass
class CapturedResponse:
    """One network response seen while the Leads page loaded."""

    url: str
    method: str
    status: int
    content_type: str
    body: str
    is_graphql: bool = False
    request_payload: str | None = None

    @property
    def path(self) -> str:
        return re.sub(r"^https?://[^/]+", "", self.url).split("?")[0]

    def parsed(self) -> Any | None:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            return None


@dataclass
class Candidate:
    """A response that might be the leads list, with the reasons why."""

    response: CapturedResponse
    score: int
    reasons: list[str] = field(default_factory=list)
    record_count: int = 0
    sample_keys: list[str] = field(default_factory=list)
    collection_path: str = ""


def score_response(response: CapturedResponse) -> Candidate | None:
    """Judge how likely a response is to be the leads collection.

    Returns ``None`` for anything that is plainly not data, so the report stays
    readable instead of listing every font and analytics beacon.
    """
    if response.status >= 400:
        return None

    payload = response.parsed()
    if payload is None:
        return None

    candidate = Candidate(response=response, score=0)

    if "json" in response.content_type.lower():
        candidate.score += 10
        candidate.reasons.append("JSON content type")

    if response.is_graphql:
        candidate.score += 15
        candidate.reasons.append("GraphQL operation")

    records, collection_path = _find_record_array(payload)
    if records is None:
        return None

    candidate.record_count = len(records)
    candidate.collection_path = collection_path
    candidate.score += 15
    candidate.reasons.append(
        f"array of {len(records)} objects at {collection_path or 'the response root'}"
    )

    keys = sorted({str(key) for record in records[:5] for key in record})
    candidate.sample_keys = keys

    matched = [hint for hint in _LEAD_FIELD_HINTS if any(hint in k.lower() for k in keys)]
    if matched:
        candidate.score += 5 * len(matched)
        candidate.reasons.append(f"lead-like fields: {', '.join(sorted(set(matched)))}")

    lowered = response.body.lower()
    if any(hint in lowered for hint in _STATUS_HINTS):
        candidate.score += 25
        candidate.reasons.append("contains a Pre Order status value")

    if "lead" in response.path.lower():
        candidate.score += 20
        candidate.reasons.append("'lead' appears in the URL path")

    return candidate


def _find_record_array(
    payload: Any,
    path: str = "",
    depth: int = 0,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Locate the first array of objects, wherever the API chose to nest it.

    APIs wrap collections differently — bare arrays, ``{"data": [...]}``,
    ``{"results": [...]}``, GraphQL's ``{"data": {"leads": {"edges": [...]}}}``.
    Rather than special-case each convention, walk the structure and take the
    first array of dictionaries found.
    """
    if depth > 6:
        return None, ""

    if isinstance(payload, list):
        objects = [item for item in payload if isinstance(item, dict)]
        if objects:
            return objects, path
        return None, ""

    if isinstance(payload, dict):
        # Prefer conventional collection keys before walking everything else,
        # so {"meta": [...], "data": [...]} does not report "meta".
        preferred = ("data", "results", "items", "leads", "records", "rows", "content")
        ordered = sorted(payload.items(), key=lambda kv: kv[0] not in preferred)

        for key, value in ordered:
            found, found_path = _find_record_array(value, f"{path}.{key}".lstrip("."), depth + 1)
            if found:
                return found, found_path

    return None, ""


def capture_leads_page(settings: Settings) -> list[CapturedResponse]:
    """Log in, open the Leads page, and record everything the page fetches."""
    from playwright.sync_api import sync_playwright

    captured: list[CapturedResponse] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=settings.headless,
            channel=settings.browser_channel or None,
        )
        context = browser.new_context()
        page = context.new_page()

        def on_response(response: Any) -> None:
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch", "document"}:
                    return

                headers = response.headers
                content_type = headers.get("content-type", "")
                if "json" not in content_type and "graphql" not in response.url.lower():
                    return

                body = response.text()
                if len(body) > _MAX_BODY_BYTES:
                    return

                captured.append(
                    CapturedResponse(
                        url=response.url,
                        method=request.method,
                        status=response.status,
                        content_type=content_type,
                        body=body,
                        is_graphql="graphql" in response.url.lower(),
                        request_payload=request.post_data,
                    )
                )
            except Exception as error:  # pragma: no cover - never break the capture loop
                logger.debug("Skipped a response", extra={"error": str(error)})

        page.on("response", on_response)

        try:
            from .auth import _await_login_result, _fill_login_form

            page.goto(settings.login_url(), wait_until="domcontentloaded")
            _fill_login_form(page, settings)
            _await_login_result(page, settings)

            logger.info("Logged in, opening the Leads page")
            page.goto(settings.leads_page_url(), wait_until="networkidle")
            # Some tables fetch lazily after first paint.
            page.wait_for_timeout(3_000)
        finally:
            context.close()
            browser.close()

    logger.info("Captured responses", extra={"count": len(captured)})
    return captured


def build_report(candidates: list[Candidate], settings: Settings) -> str:
    """Render the findings as Markdown, ready to paste into ARCHITECTURE.md."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Leads endpoint discovery",
        "",
        f"Captured {now} against `{settings.portal_base_url}`.",
        "",
    ]

    if not candidates:
        lines.extend(
            [
                "**No JSON endpoint found.**",
                "",
                "The Leads page returned no JSON response containing an array of objects.",
                "That points to a server-rendered table, so set `LEADS_CLIENT=dom` and the",
                "Playwright client will parse the rendered page instead.",
                "",
            ]
        )
        return "\n".join(lines)

    best = candidates[0]
    lines.extend(
        [
            "## Recommendation",
            "",
            f"Use the API client against `{best.response.path}`.",
            "",
            "```ini",
            "LEADS_CLIENT=api",
            f"LEADS_API_PATH={best.response.path}",
            "```",
            "",
            f"Collection is at `{best.collection_path or 'the response root'}` "
            f"with {best.record_count} records on the first page.",
            "",
            "Available fields:",
            "",
            "```",
            "\n".join(best.sample_keys),
            "```",
            "",
            "## All candidates",
            "",
        ]
    )

    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"### {index}. `{candidate.response.method} {candidate.response.path}` "
                f"(score {candidate.score})",
                "",
                f"- Status: {candidate.response.status}",
                f"- Content type: {candidate.response.content_type}",
                f"- Records: {candidate.record_count}",
                f"- Why: {'; '.join(candidate.reasons)}",
                "",
            ]
        )

    return "\n".join(lines)


def run_discovery(settings: Settings, output_dir: Path) -> Path:
    """Capture, score, and write the report. Returns the report path."""
    output_dir.mkdir(parents=True, exist_ok=True)

    responses = capture_leads_page(settings)

    candidates = [c for c in (score_response(r) for r in responses) if c is not None]
    candidates.sort(key=lambda c: c.score, reverse=True)

    report_path = output_dir / "leads-endpoint.md"
    report_path.write_text(build_report(candidates, settings), encoding="utf-8")

    # The raw bodies hold real personal data, so they stay in the gitignored
    # discovery directory and are never part of the report itself.
    raw_path = output_dir / "raw-responses.json"
    raw_path.write_text(
        json.dumps(
            [
                {
                    "url": r.url,
                    "method": r.method,
                    "status": r.status,
                    "content_type": r.content_type,
                    "request_payload": r.request_payload,
                    "body": r.body[:20_000],
                }
                for r in responses
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    raw_path.chmod(0o600)

    logger.info("Discovery report written", extra={"path": str(report_path)})
    return report_path
