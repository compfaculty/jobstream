"""Company identity and bounded enrichment from operator-verified official websites."""

import hashlib
import http.client
import json
import unicodedata
import uuid
import warnings
from io import BytesIO
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from jobs.models import Company, Source

from .network import canonical_url, fetch_public_document


def company_key(name: str, scope: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    return hashlib.sha256(f"{scope}:{normalized}".encode()).hexdigest()


def resolve_company(name: str, source: Source) -> Company | None:
    """Only an explicit company career endpoint can identify an employer record."""
    if source.kind == Source.Kind.BOARD:
        return None
    if source.company_id is None:
        raise ValueError("Company career source has no company")
    return source.company


def website_metadata(data: bytes, base_url: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(data, "html.parser")
    description = soup.find("meta", attrs={"name": "description"})
    text = str(description.get("content", ""))[:5000] if description else ""
    logo = ""
    for script in soup.find_all("script", type="application/ld+json")[:30]:
        try:
            value = json.loads(script.string or "")
        except ValueError, TypeError, RecursionError:
            continue
        nodes = value if isinstance(value, list) else [value]
        # Limit traversal even for hostile or malformed JSON-LD.
        for _ in range(100):
            if not nodes:
                break
            node = nodes.pop(0)
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(graph[:100])
            kind = node.get("@type", [])
            kind = [kind] if isinstance(kind, str) else kind
            if not isinstance(kind, list) or not {"Organization", "Corporation"}.intersection(
                item for item in kind if isinstance(item, str)
            ):
                continue
            candidate = node.get("logo")
            if isinstance(candidate, dict):
                candidate = candidate.get("contentUrl") or candidate.get("url")
            if isinstance(candidate, str) and candidate:
                logo = candidate
                break
        if logo:
            break
    candidates = [logo] if logo else []
    icons = soup.find_all("link", rel=lambda rel: rel and "icon" in rel.lower())[:30]
    # Prefer raster touch icons over tiny favicons or unsupported SVG assets.
    icons.sort(key=lambda icon: "apple-touch-icon" not in icon.get("rel", []))
    candidates.extend(icon["href"] for icon in icons if isinstance(icon.get("href"), str))
    logos = []
    for candidate in candidates:
        if urlsplit(candidate).path.lower().endswith(".svg"):
            continue
        url = canonical_url(urljoin(base_url, candidate))
        if len(url) > 2048:
            raise ValueError("Logo URL exceeds limit")
        if url not in logos:
            logos.append(url)
        if len(logos) == 3:
            break
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True), logos


def normalize_logo(data: bytes) -> bytes:
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Logo exceeds byte limit")
    # Decode and re-encode raster pixels; never store remote SVG, HTML, or metadata.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 4_000_000:
                raise ValueError("Logo exceeds pixel limit")
            if image.format not in {"PNG", "JPEG", "WEBP", "ICO", "GIF"}:
                raise ValueError("Unsupported logo format")
            image.load()
            result = image.convert("RGBA")
            result.thumbnail((256, 256))
            result.info.clear()  # Re-encoding alone can preserve source EXIF/ICC metadata.
            output = BytesIO()
            result.save(output, format="PNG")
            return output.getvalue()


def enrich_company(
    company: Company, *, dry_run: bool = False, retries: int = 1
) -> dict[str, str | bool]:
    if not 0 <= retries <= 3:
        raise ValueError("Retries must be 0–3")
    if not company.website or not company.website_verified:
        raise ValueError("Company requires an operator-verified official website")
    website = canonical_url(company.website)

    def fetch(url):
        for attempt in range(retries + 1):
            try:
                return fetch_public_document(url)
            except OSError, http.client.HTTPException:
                if attempt == retries:
                    raise

    data, effective_url = fetch(website)
    description, candidates = website_metadata(data, effective_url)
    logo = None
    logo_url = ""
    last_error = None
    for candidate in candidates:
        try:
            logo = normalize_logo(fetch(candidate)[0])
        except ENRICHMENT_ERRORS as exc:
            last_error = exc
            continue
        logo_url = candidate
        break
    if candidates and logo is None:
        raise ValueError("No valid raster company logo") from last_error
    changes = {"description": description, "logo_url": logo_url, "has_logo": logo is not None}
    if dry_run:
        return changes
    from .discovery import from_html

    from_html(data, effective_url, company.name)
    old_logo = company.logo.name
    new_logo = None
    try:
        if logo is not None:
            company.logo.save(f"{uuid.uuid4().hex}.png", ContentFile(logo), save=False)
            new_logo = company.logo.name
            company.logo_source_url = logo_url
        # Preserve curated descriptions; the command only fills missing copy.
        if not company.description:
            company.description = description
        company.enriched_at = company.enrichment_attempted_at = timezone.now()
        company.enrichment_error = ""
        company.save(
            update_fields=[
                "logo",
                "logo_source_url",
                "description",
                "enriched_at",
                "enrichment_attempted_at",
                "enrichment_error",
            ]
        )
    except Exception:
        if new_logo:
            company.logo.storage.delete(new_logo)
        raise
    if new_logo and old_logo and old_logo != new_logo:
        company.logo.storage.delete(old_logo)
    return changes


ENRICHMENT_ERRORS = (
    ValueError,
    OSError,
    http.client.HTTPException,
    UnidentifiedImageError,
    Image.DecompressionBombWarning,
    Image.DecompressionBombError,
)
