"""Read contact signals out of one HTML page. Nothing here is site-specific."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from contact_crawler.cleaner import extract_emails, prepare_text, registrable_domain

_INLINE_TAGS = {
    "span", "a", "b", "strong", "em", "i", "u", "small", "font",
    "label", "abbr", "sup", "sub", "nobr",
}

_CONTACT_HINTS = (
    (re.compile(r"contatti|kontakt|contact", re.IGNORECASE), 50),
    (re.compile(r"get[-_\s]?in[-_\s]?touch|reach[-_\s]?us", re.IGNORECASE), 45),
    (re.compile(r"office", re.IGNORECASE), 32),
    (re.compile(r"location|hours", re.IGNORECASE), 22),
    (re.compile(r"enquir|inquir|support", re.IGNORECASE), 18),
    (re.compile(r"\babout(?:[-_\s]?us)?\b", re.IGNORECASE), 8),
)

_SKIP_LINK = re.compile(
    r"privacy|terms|cookie|login|sign[-_]?in|cart|wp-admin|account|javascript:",
    re.IGNORECASE,
)

_PHONE_RES = (
    re.compile(r"\+\d{1,3}(?:[\s.\-()*]*\d){7,14}"),
    re.compile(r"\(\d{3}\)\s*\d{3}[\s.\-]?\d{4}"),
    re.compile(r"\b1[\s.\-]\d{3}[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    re.compile(r"\b\d{3}[\s.\-]\d{3}[\s.\-]\d{4}\b"),
)

_MAILTO_RE = re.compile(r"mailto:([^\"'\s>#]+)", re.IGNORECASE)
_TEL_RE = re.compile(r"tel:([^\"'\s>#]+)", re.IGNORECASE)
_CF_RE = re.compile(r"data-cfemail=[\"']([0-9a-fA-F]+)[\"']", re.IGNORECASE)
_CF_HREF_RE = re.compile(r"email-protection#([0-9a-fA-F]+)", re.IGNORECASE)

_STREET_RE = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'\-]+\s){0,6}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct)\b",
    re.IGNORECASE,
)
_VIA_RE = re.compile(
    r"\bVia\s+[A-Z][A-Za-zÀ-ÿ .'\-]{2,40}?\s+\d{1,5}"
    r"(?:,\s*[A-Za-zÀ-ÿ .'\-]{2,40}){0,2}",
)
_POSTAL_RE = re.compile(r"\b\d{4,6}(?:-\d{4})?\b")

PROBE_PATHS = (
    "/contact",
    "/contact-us",
    "/contactus.html",
    "/contact.html",
    "/get-in-touch",
    "/about-us",
    "/about",
    "/locations",
    "/contatti",
    "/kontakt",
)


@dataclass
class Link:
    href: str
    text: str


@dataclass
class ParsedPage:
    title: str = ""
    meta: list[tuple[str, str]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    jsonld: list[object] = field(default_factory=list)
    jsonld_raw: list[str] = field(default_factory=list)
    visible_text: str = ""
    mailtos: list[str] = field(default_factory=list)
    tels: list[str] = field(default_factory=list)
    cfemails: list[str] = field(default_factory=list)
    attribute_emails: list[str] = field(default_factory=list)
    refresh_url: str | None = None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage()
        self._in_title = False
        self._title_parts: list[str] = []
        self._skip = 0
        self._in_script = False
        self._script_type = ""
        self._script_buf: list[str] = []
        self._anchor: str | None = None
        self._anchor_text: list[str] = []
        self._visible: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
            if tag == "script":
                self._in_script = True
                self._script_type = ad.get("type", "")
                self._script_buf = []
        if tag == "title" and self._skip == 0:
            self._in_title = True
            self._title_parts = []
        if tag == "a" and self._skip == 0:
            self._anchor = ad.get("href", "")
            self._anchor_text = []
            self._take_href(self._anchor)
        if tag == "meta":
            self._take_meta(ad)
        if tag == "br" and self._skip == 0:
            self._visible.append("\n")
        cf = ad.get("data-cfemail", "")
        if cf:
            self.page.cfemails.append(cf)
        href = ad.get("href", "")
        if "email-protection#" in href.lower():
            self.page.cfemails.append(href.split("#", 1)[-1])
        for key, value in ad.items():
            if key in {"data-email", "data-mail"} and value:
                self.page.attribute_emails.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            text = re.sub(r"\s+", " ", "".join(self._anchor_text)).strip()
            self.page.links.append(Link(self._anchor, text))
            self._anchor = None
        if tag == "title":
            self._in_title = False
            self.page.title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
            if tag == "script" and self._in_script:
                if "ld+json" in self._script_type.lower():
                    self.page.jsonld_raw.append("".join(self._script_buf))
                self._in_script = False
        if self._skip == 0 and tag not in _INLINE_TAGS and tag not in {"script", "style", "noscript"}:
            self._visible.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_script:
            self._script_buf.append(data)
        if self._skip == 0:
            self._visible.append(data)
            if self._anchor is not None:
                self._anchor_text.append(data)

    def finish(self) -> ParsedPage:
        self.page.visible_text = "".join(self._visible)
        return self.page

    def _take_href(self, href: str) -> None:
        lower = href.lower()
        if lower.startswith("mailto:"):
            value = unquote(href[7:].split("?", 1)[0]).strip()
            if value:
                self.page.mailtos.append(value)
        elif lower.startswith("tel:"):
            value = unquote(href[4:].split("?", 1)[0]).strip()
            if value:
                self.page.tels.append(value)

    def _take_meta(self, ad: dict[str, str]) -> None:
        http_equiv = ad.get("http-equiv", "").lower()
        content = ad.get("content", "")
        if http_equiv == "refresh" and content:
            match = re.search(r"url\s*=\s*['\"]?([^'\";>]+)", content, re.IGNORECASE)
            if match:
                self.page.refresh_url = match.group(1).strip()
        name = (ad.get("name") or ad.get("property") or ad.get("itemprop") or "").lower()
        if name and content:
            self.page.meta.append((name, content))


def parse_html(html: str) -> ParsedPage:
    parser = _Parser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    page = parser.finish()
    _merge_raw_signals(page, html or "")
    page.jsonld = _load_jsonld(page.jsonld_raw)
    return page


def _merge_raw_signals(page: ParsedPage, html: str) -> None:
    for match in _MAILTO_RE.finditer(html):
        value = unquote(match.group(1).split("?", 1)[0]).strip()
        if value and value not in page.mailtos:
            page.mailtos.append(value)
    for match in _TEL_RE.finditer(html):
        value = unquote(match.group(1).split("?", 1)[0]).strip()
        if value and value not in page.tels:
            page.tels.append(value)
    for match in _CF_RE.finditer(html):
        if match.group(1) not in page.cfemails:
            page.cfemails.append(match.group(1))
    for match in _CF_HREF_RE.finditer(html):
        if match.group(1) not in page.cfemails:
            page.cfemails.append(match.group(1))


def _load_jsonld(blocks: list[str]) -> list[object]:
    loaded: list[object] = []
    for block in blocks:
        text = block.strip()
        if not text:
            continue
        try:
            loaded.append(json.loads(text))
            continue
        except json.JSONDecodeError:
            pass
        trimmed = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            loaded.append(json.loads(trimmed))
        except json.JSONDecodeError:
            loaded.append({"_raw": text})
    return loaded


def meta_email_text(page: ParsedPage) -> str:
    chunks: list[str] = []
    for name, content in page.meta:
        if name in {"description", "og:description", "twitter:description"} or "email" in name:
            chunks.append(content)
    return "\n".join(chunks)


def iter_jsonld_contacts(node: object):
    """Yield ('email'|'phone'|'address', value) from schema.org JSON."""
    if isinstance(node, list):
        for item in node:
            yield from iter_jsonld_contacts(item)
        return
    if not isinstance(node, dict):
        return
    if "_raw" in node and len(node) == 1:
        return
    for key in ("email", "emailAddress", "contactEmail"):
        value = node.get(key)
        if isinstance(value, str):
            yield ("email", value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield ("email", item)
    telephone = node.get("telephone") or node.get("telephoneNumber")
    if isinstance(telephone, str):
        yield ("phone", telephone)
    elif isinstance(telephone, list):
        for item in telephone:
            if isinstance(item, str):
                yield ("phone", item)
    if _is_address(node):
        formatted = format_address(node)
        if formatted:
            yield ("address", formatted)
    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from iter_jsonld_contacts(value)


def _is_address(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(isinstance(item, str) and "PostalAddress" in item for item in kinds):
        return True
    return "streetAddress" in node


def _as_text(value: object) -> str:
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            return ""
        return value.strip()
    if isinstance(value, dict):
        return _as_text(value.get("name") or "")
    return ""


def format_address(node: dict) -> str:
    parts = [
        _as_text(node.get(key))
        for key in (
            "streetAddress",
            "postOfficeBoxNumber",
            "addressLocality",
            "addressRegion",
            "postalCode",
            "addressCountry",
        )
    ]
    return ", ".join(part for part in parts if part)


def extract_phones(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _PHONE_RES:
        for match in pattern.finditer(text or ""):
            display = re.sub(r"\s+", " ", match.group(0)).strip(" .,;")
            digits = re.sub(r"\D", "", display)
            if not 8 <= len(digits) <= 15 or len(set(digits)) < 3:
                continue
            if digits in seen:
                continue
            seen.add(digits)
            found.append(display)
    return found


def extract_addresses(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    flat = re.sub(r"\s+", " ", prepare_text(text or ""))
    for match in _STREET_RE.finditer(flat):
        tail = flat[match.end() : match.end() + 90]
        postal = _pick_postal(tail)
        if not postal:
            continue
        line = flat[match.start() : match.end() + postal.end()]
        line = re.sub(r"\s+", " ", line).strip(" ,.")
        line = re.sub(
            r"(\b(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct)\.?)\s+&\s+",
            r"\1, ",
            line,
            flags=re.IGNORECASE,
        )
        _add_unique_address(found, seen, line)
    for match in _VIA_RE.finditer(flat):
        line = re.sub(r"\s+", " ", match.group(0)).strip(" ,")
        _add_unique_address(found, seen, line)
    return found


def _pick_postal(tail: str):
    """Choose a postal code, not a suite number or a piece of a phone."""
    best = None
    best_score = 0
    for match in _POSTAL_RE.finditer(tail):
        prefix = tail[max(0, match.start() - 18) : match.start()]
        if re.search(r"[\d\-]\s*$", prefix):
            continue
        score = 1
        if re.search(r"\b[A-Za-z]{2}\s*$", prefix):
            score += 5
        if "," in prefix:
            score += 2
        if len(match.group(0)) == 5:
            score += 2
        if re.search(r"\b(?:suite|ste|apt|unit|floor)\b\s*$", prefix, re.IGNORECASE):
            score -= 5
        if score > best_score:
            best_score = score
            best = match
    return best


def _add_unique_address(found: list[str], seen: set[str], line: str) -> None:
    if len(line) < 8 or len(line) > 180:
        return
    key = line.lower()
    if key in seen:
        return
    seen.add(key)
    found.append(line)


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    netloc = parsed.netloc.lower()
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def discover_contact_urls(page_url: str, links: list[Link], limit: int = 6) -> list[tuple[int, str]]:
    """Rank same-site links that look like a contact, location, or about page."""
    base = urlparse(page_url)
    base_root = registrable_domain(base.netloc)
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for link in links:
        href = (link.href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if base_root and registrable_domain(parsed.netloc) != base_root:
            continue
        if parsed.fragment and canonical_url(absolute) == canonical_url(page_url):
            continue
        blob = f"{parsed.path} {link.text}"
        if _SKIP_LINK.search(blob):
            continue
        score = sum(weight for pattern, weight in _CONTACT_HINTS if pattern.search(blob))
        if score <= 0:
            continue
        cleaned = canonical_url(absolute)
        if cleaned in seen or cleaned == canonical_url(page_url):
            continue
        seen.add(cleaned)
        ranked.append((score, cleaned))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[:limit]


def probe_urls(page_url: str) -> list[str]:
    parsed = urlparse(page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return [canonical_url(origin + path) for path in PROBE_PATHS]


def looks_blocked(html: str, status: int, title: str) -> bool:
    sample = (html or "")[:4000].lower()
    title_l = (title or "").lower()
    if "just a moment" in title_l or "attention required" in title_l:
        return True
    if "cf-browser-verification" in sample or "cf-challenge" in sample:
        return True
    if status in {401, 403, 429, 503} and "cloudflare" in sample:
        return True
    return False


def looks_unavailable(html: str, status: int, title: str) -> bool:
    title_l = (title or "").lower()
    sample = (html or "")[:2500].lower()
    if "connectyourdomain" in title_l or "connect your domain" in sample:
        return True
    if "wordpress › error" in title_l or "wordpress > error" in title_l:
        return True
    if "domain for sale" in sample or "buy this domain" in sample or "parked domain" in sample:
        return True
    if status in {404, 410} and len(html or "") < 20000:
        return True
    if "page not found" in title_l and status >= 400:
        return True
    return False