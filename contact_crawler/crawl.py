"""Crawl one site: homepage, then a contact page when the homepage is thin."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse

from contact_crawler.cleaner import (
    SOFT_PENALTY_LOCALS,
    decode_cloudflare_email,
    domain_affinity,
    extract_emails,
    registrable_domain,
    score_email,
)
from contact_crawler.extract import (
    ParsedPage,
    canonical_url,
    discover_contact_urls,
    extract_addresses,
    extract_phones,
    iter_jsonld_contacts,
    looks_blocked,
    looks_unavailable,
    meta_email_text,
    parse_html,
    probe_urls,
)
from contact_crawler.fetch import FetchResult, Fetcher

_PHONE_RANK = {"tel": 3, "jsonld": 2, "text": 1}


@dataclass
class EmailHit:
    email: str
    source: str
    page: str
    score: int
    low_value: bool


@dataclass
class PhoneHit:
    phone: str
    source: str
    page: str


@dataclass
class SiteResult:
    query: str
    final_url: str | None
    status: str
    http_status: int | None
    title: str | None
    best_email: str | None
    emails: list[EmailHit] = field(default_factory=list)
    phones: list[PhoneHit] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    pages_fetched: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def crawl_target(
    query: str,
    fetcher: Fetcher | None = None,
    max_contact_pages: int = 4,
    probe: bool = True,
) -> SiteResult:
    """Fetch one domain or URL and return the public contact details on it."""
    client = fetcher or Fetcher()
    result = SiteResult(
        query=query.strip(),
        final_url=None,
        status="unreachable",
        http_status=None,
        title=None,
        best_email=None,
    )
    home = _fetch_homepage(result.query, client)
    if home is None:
        result.notes.append("no response from https or http")
        return result

    home = _follow_refresh(home, client, result.notes)
    result.final_url = home.final_url
    result.http_status = home.status or None
    if home.insecure_tls:
        result.notes.append("insecure_tls")
    if home.via_proxy:
        result.notes.append("fetched_via_zyte_proxy")

    parsed = parse_html(home.html)
    result.title = parsed.title or None
    result.status = _classify(home, parsed)
    result.pages_fetched.append(home.final_url)

    query_host = _host_of(result.query)
    final_host = urlparse(home.final_url).netloc
    if (
        query_host
        and final_host
        and registrable_domain(query_host) != registrable_domain(final_host)
    ):
        result.notes.append(f"redirected_to:{home.final_url}")

    if result.status in {"blocked", "unreachable"}:
        return result

    hosts = [host for host in (query_host, final_host) if host]
    emails: dict[str, EmailHit] = {}
    phones: dict[str, PhoneHit] = {}
    addresses: list[str] = []
    _collect(parsed, home.final_url, hosts, emails, phones, addresses)

    ranked = discover_contact_urls(home.final_url, parsed.links)
    seen_links = {url for _, url in ranked}
    result.contact_links = [url for _, url in ranked]

    if result.status == "ok":
        _visit_contact_pages(
            start_url=home.final_url,
            ranked=ranked,
            seen_links=seen_links,
            client=client,
            hosts=hosts,
            emails=emails,
            phones=phones,
            addresses=addresses,
            result=result,
            max_contact_pages=max_contact_pages,
            probe=probe,
        )

    _drop_demo_rows(emails, hosts)
    result.emails = sorted(emails.values(), key=lambda hit: (-hit.score, hit.email))[:25]
    result.phones = _dedupe_phones(phones)[:12]
    result.addresses = addresses[:5]
    result.best_email = _best(result.emails)
    if result.status == "ok" and not result.emails:
        result.notes.append("no_email_found")
    elif result.emails and all(hit.low_value for hit in result.emails):
        result.notes.append("only_low_value_email")
    return result


def _fetch_page(client: Fetcher, url: str) -> FetchResult:
    """Fetch directly. Use Zyte only when that attempt is blocked or gets no page back."""
    result = client.fetch(url)
    if not _needs_proxy(result) or not getattr(client, "proxy_enabled", False):
        return result
    proxied = client.fetch(url, via_proxy=True)
    if proxied.status == 200 and proxied.html and not _result_is_blocked(proxied):
        proxied.via_proxy = True
        return proxied
    return result


def _needs_proxy(result: FetchResult) -> bool:
    if _result_is_blocked(result):
        return True
    # A dropped connection is not proof the site is down.
    return result.status == 0 or not result.html


def _result_is_blocked(result: FetchResult) -> bool:
    title = ""
    if result.html:
        title = parse_html(result.html).title
    return looks_blocked(result.html, result.status, title)


def _fetch_homepage(query: str, client: Fetcher) -> FetchResult | None:
    best: FetchResult | None = None
    for url in _start_urls(query):
        fetched = _fetch_page(client, url)
        if best is None:
            best = fetched
        if fetched.status == 200 and len(fetched.html) > 40:
            return fetched
        parsed = parse_html(fetched.html) if fetched.html else None
        if parsed and looks_blocked(fetched.html, fetched.status, parsed.title):
            return fetched
        if fetched.status and len(fetched.html) > len(best.html):
            best = fetched
    return best if best and (best.status or best.html) else None


def _start_urls(query: str) -> list[str]:
    text = query.strip()
    if "://" in text:
        return [text]
    host = text.strip("/")
    hosts = [host]
    if not host.lower().startswith("www."):
        hosts.append("www." + host)
    urls: list[str] = []
    for scheme in ("https", "http"):
        for name in hosts:
            urls.append(f"{scheme}://{name}/")
    return urls


def _follow_refresh(page: FetchResult, client: Fetcher, notes: list[str]) -> FetchResult:
    current = page
    seen = {canonical_url(current.final_url)}
    for _ in range(2):
        parsed = parse_html(current.html)
        if not parsed.refresh_url:
            return current
        nxt = canonical_url(urljoin(current.final_url, parsed.refresh_url))
        if nxt in seen:
            return current
        seen.add(nxt)
        notes.append(f"meta_refresh:{nxt}")
        refreshed = _fetch_page(client, nxt)
        if refreshed.status == 0 and not refreshed.html:
            return current
        current = refreshed
    return current


def _classify(page: FetchResult, parsed: ParsedPage) -> str:
    if page.status == 0 and not page.html:
        return "unreachable"
    if looks_blocked(page.html, page.status, parsed.title):
        return "blocked"
    if looks_unavailable(page.html, page.status, parsed.title):
        return "unavailable"
    if page.status >= 400:
        return "error"
    if page.status == 200 and len(page.html.strip()) < 30:
        return "empty"
    return "ok"


def _visit_contact_pages(
    start_url: str,
    ranked: list[tuple[int, str]],
    seen_links: set[str],
    client: Fetcher,
    hosts: list[str],
    emails: dict[str, EmailHit],
    phones: dict[str, PhoneHit],
    addresses: list[str],
    result: SiteResult,
    max_contact_pages: int,
    probe: bool,
) -> None:
    queue = list(ranked)
    fetched = {canonical_url(start_url)}
    budget = max_contact_pages
    contact_fetches = 0

    while queue and budget > 0:
        if _usable(emails, hosts) and contact_fetches >= 1:
            break
        score, url = queue.pop(0)
        if _usable(emails, hosts) and score < 40:
            break
        if url in fetched:
            continue
        fetched.add(url)
        budget -= 1
        page = _fetch_page(client, url)
        parsed = parse_html(page.html)
        if not _page_is_useful(page, parsed):
            continue
        if page.via_proxy and "fetched_via_zyte_proxy" not in result.notes:
            result.notes.append("fetched_via_zyte_proxy")
        contact_fetches += 1
        result.pages_fetched.append(page.final_url)
        _collect(parsed, page.final_url, hosts, emails, phones, addresses)
        if _usable(emails, hosts):
            continue
        for child_score, child in discover_contact_urls(page.final_url, parsed.links):
            if child not in seen_links and child not in fetched:
                seen_links.add(child)
                result.contact_links.append(child)
                queue.append((child_score, child))
        queue.sort(key=lambda item: -item[0])

    if probe and budget > 0 and not _usable(emails, hosts):
        for url in probe_urls(result.final_url or start_url):
            if budget <= 0 or _usable(emails, hosts):
                break
            if url in fetched:
                continue
            fetched.add(url)
            budget -= 1
            page = _fetch_page(client, url)
            parsed = parse_html(page.html)
            if not _page_is_useful(page, parsed):
                continue
            result.pages_fetched.append(page.final_url)
            if url not in result.contact_links:
                result.contact_links.append(url)
            _collect(parsed, page.final_url, hosts, emails, phones, addresses)


def _page_is_useful(page: FetchResult, parsed: ParsedPage) -> bool:
    if page.status != 200 or len(page.html) < 40:
        return False
    if looks_blocked(page.html, page.status, parsed.title):
        return False
    title = (parsed.title or "").lower()
    if "page not found" in title or title.strip() in {"404", "not found"}:
        return False
    return True


def _usable(emails: dict[str, EmailHit], hosts: list[str]) -> bool:
    for hit in emails.values():
        if hit.low_value:
            continue
        local = hit.email.split("@", 1)[0]
        if local in SOFT_PENALTY_LOCALS:
            continue
        if domain_affinity(hit.email, hosts) == "other":
            continue
        return True
    return False


def _collect(
    parsed: ParsedPage,
    page_url: str,
    hosts: list[str],
    emails: dict[str, EmailHit],
    phones: dict[str, PhoneHit],
    addresses: list[str],
) -> None:
    for raw in parsed.mailtos:
        for email in extract_emails(raw):
            _add_email(emails, email, "mailto", page_url, hosts)
    for encoded in parsed.cfemails:
        email = decode_cloudflare_email(encoded)
        if email:
            _add_email(emails, email, "cfemail", page_url, hosts)
    for node in parsed.jsonld:
        for kind, value in iter_jsonld_contacts(node):
            if kind == "email":
                for email in extract_emails(value):
                    _add_email(emails, email, "jsonld", page_url, hosts)
            elif kind == "phone":
                for phone in extract_phones(value) or [_clean_phone(value)]:
                    if phone:
                        _add_phone(phones, phone, "jsonld", page_url)
            elif kind == "address":
                _add_address(addresses, value)
        if isinstance(node, dict) and "_raw" in node:
            for email in extract_emails(str(node["_raw"])):
                _add_email(emails, email, "jsonld", page_url, hosts)
    for email in extract_emails(meta_email_text(parsed)):
        _add_email(emails, email, "meta", page_url, hosts)
    for raw in parsed.attribute_emails:
        for email in extract_emails(raw):
            _add_email(emails, email, "text", page_url, hosts)
    for email in extract_emails(parsed.visible_text):
        _add_email(emails, email, "text", page_url, hosts)
    for raw in parsed.tels:
        phone = _clean_phone(raw)
        if phone:
            _add_phone(phones, phone, "tel", page_url)
    for phone in extract_phones(parsed.visible_text):
        _add_phone(phones, phone, "text", page_url)
    for address in extract_addresses(parsed.visible_text):
        _add_address(addresses, address)


def _add_email(
    store: dict[str, EmailHit],
    email: str,
    source: str,
    page: str,
    hosts: list[str],
) -> None:
    score, low = score_email(email, source, hosts)
    current = store.get(email)
    if current is None or score > current.score:
        store[email] = EmailHit(email, source, page, score, low)


def _clean_phone(raw: str) -> str:
    display = re.sub(r"\s+", " ", raw or "").strip()
    digits = re.sub(r"\D", "", display)
    if not 8 <= len(digits) <= 15 or len(set(digits)) < 3:
        return ""
    return display


def _add_phone(store: dict[str, PhoneHit], phone: str, source: str, page: str) -> None:
    display = _clean_phone(phone)
    if not display:
        return
    digits = re.sub(r"\D", "", display)
    current = store.get(digits)
    if current is None:
        store[digits] = PhoneHit(display, source, page)
        return
    source_rank = _PHONE_RANK.get(source, 0)
    current_rank = _PHONE_RANK.get(current.source, 0)
    chosen_source = source if source_rank >= current_rank else current.source
    chosen_page = page if source_rank >= current_rank else current.page
    chosen_display = display if _pretty(display) >= _pretty(current.phone) else current.phone
    store[digits] = PhoneHit(chosen_display, chosen_source, chosen_page)


def _pretty(phone: str) -> int:
    return phone.count(" ") + phone.count("-") + phone.count("(")


def _drop_demo_rows(emails: dict[str, EmailHit], hosts: list[str]) -> None:
    """Drop a block of first.last@mail.com rows when the site has its own inbox.

    A single unrelated address is kept. Aerial Dragons publishes a sample
    table of four mail.com addresses next to its real bookings address.
    """
    demos = [
        email
        for email in emails
        if domain_affinity(email, hosts) == "other"
        and email.split("@", 1)[1] == "mail.com"
        and "." in email.split("@", 1)[0]
    ]
    has_site_email = any(domain_affinity(email, hosts) != "other" for email in emails)
    if has_site_email and len(demos) >= 2:
        for email in demos:
            del emails[email]


def _dedupe_phones(store: dict[str, PhoneHit]) -> list[PhoneHit]:
    keys = sorted(store, key=len, reverse=True)
    kept: list[str] = []
    for key in keys:
        if any(other != key and other.endswith(key) and len(other) - len(key) <= 3 for other in kept):
            continue
        kept.append(key)
    hits = [store[key] for key in kept]
    hits.sort(key=lambda hit: (-_PHONE_RANK.get(hit.source, 0), hit.phone))
    return hits


def _add_address(items: list[str], address: str) -> None:
    cleaned = re.sub(r"\s+", " ", address).strip(" ,")
    if len(cleaned) < 8 or len(cleaned) > 180:
        return
    if cleaned.lower() in {item.lower() for item in items}:
        return
    items.append(cleaned)


def _best(emails: list[EmailHit]) -> str | None:
    if not emails:
        return None
    preferred = [hit for hit in emails if not hit.low_value]
    return (preferred or emails)[0].email


def _host_of(query: str) -> str:
    text = query.strip()
    if "://" in text:
        return urlparse(text).netloc
    return text.strip("/")
