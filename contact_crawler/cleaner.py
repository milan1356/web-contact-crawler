"""Turn messy public email text into a normal address.

Handles the cases a page actually uses: "name at company dot com",
"[at]" / "(at)" / "{at}", the same forms for "dot", spaced "@" and ".",
and HTML entities such as &#64;. Junk filenames and placeholders are dropped.
"""

from __future__ import annotations

import html
import re

JUNK_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "ico",
    "woff", "woff2", "ttf", "eot", "map", "pdf", "zip", "mp4", "mp3",
    "webm", "avif", "bmp", "html", "htm", "php", "asp", "aspx", "json",
}

PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net",
    "domain.com", "email.com", "yourdomain.com",
    "test.com", "invalid.com", "localhost",
}

PLACEHOLDER_LOCALS = {
    "user", "username", "name", "yourname", "email",
    "example", "test", "foo", "bar", "someone", "you",
    "your", "abc", "xyz",
}

DENY_DOMAIN_SUFFIXES = (
    "sentry.io",
    "wixpress.com",
    "schema.org",
    "w3.org",
    "googleapis.com",
    "gstatic.com",
    "cloudflare.com",
    "myshopify.com",
    "gravatar.com",
    "wordpress.org",
    "w.org",
)

LOW_VALUE_LOCALS = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "bounce",
    "mailer-daemon",
    "postmaster",
}

SOFT_PENALTY_LOCALS = {"privacy", "webmaster", "hostmaster", "abuse"}

ROLE_BONUS_LOCALS = {
    "info", "hello", "contact", "sales", "bookings", "booking",
    "office", "enquiries", "inquiries", "support", "admin",
}

SOURCE_WEIGHT = {
    "mailto": 50,
    "cfemail": 48,
    "jsonld": 42,
    "text": 34,
    "meta": 28,
}

MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "com.br", "co.in", "com.mx",
    "co.za", "com.sg", "com.tr", "co.jp",
}

_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}"
    r"\s*(?:@|\[at\]|\(at\)|\{at\}|\bat\b)\s*"
    r"[A-Za-z0-9][A-Za-z0-9\-]*"
    r"(?:\s*(?:\.|\[dot\]|\(dot\)|\{dot\}|\bdot\b)\s*[A-Za-z0-9][A-Za-z0-9\-]*){1,4}"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_AT_RE = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\}|\bat\b)\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\bdot\b)\s*", re.IGNORECASE)
_SPACE_AT_RE = re.compile(r"\s*@\s*")
_SPACE_DOT_RE = re.compile(r"\s*\.\s*")

_TRAILING_WORDS = {
    "contact", "contacts", "email", "emails", "phone", "phones",
    "call", "please", "the", "and", "for", "our", "you", "your",
    "page", "home", "click", "here", "mailto", "tel", "www",
}


def prepare_text(text: str) -> str:
    cleaned = html.unescape(text or "")
    return (
        cleaned.replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\xa0", " ")
    )


def is_plausible_email(email: str) -> bool:
    if not email or email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain or len(local) > 64 or len(domain) > 255:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local or ".." in domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or domain.startswith("-"):
        return False
    if not re.fullmatch(r"[a-z0-9._%+\-]+", local):
        return False
    if not re.fullmatch(r"[a-z0-9.\-]+", domain):
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    tld = labels[-1]
    if tld in JUNK_TLDS or not re.fullmatch(r"[a-z]{2,24}", tld):
        return False
    for label in labels:
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return False
    if not re.search(r"[a-z]", local) or not re.search(r"[a-z]", domain):
        return False
    if local in PLACEHOLDER_LOCALS or domain in PLACEHOLDER_DOMAINS:
        return False
    if any(domain == suffix or domain.endswith("." + suffix) for suffix in DENY_DOMAIN_SUFFIXES):
        return False
    return True


def normalize_email(raw: str) -> str | None:
    """Clean one candidate. Returns None when it is not a real address."""
    text = prepare_text(raw).strip()
    text = _AT_RE.sub("@", text)
    text = _DOT_RE.sub(".", text)
    text = _SPACE_AT_RE.sub("@", text)
    text = _SPACE_DOT_RE.sub(".", text)
    text = text.strip(".,;:<>()[]{}\"' \t\r\n")
    text = text.lower()
    text = _strip_sentence_tail(raw, text)
    if text.count("@") != 1 or not is_plausible_email(text):
        return None
    return text


def _strip_sentence_tail(raw: str, email: str) -> str:
    if email.count("@") != 1:
        return email
    local, domain = email.split("@", 1)
    labels = domain.split(".")
    if len(labels) < 3 or labels[-1] not in _TRAILING_WORDS:
        return email
    if not re.search(r"[.\]]\s+" + re.escape(labels[-1]) + r"\b", raw, re.IGNORECASE):
        return email
    return f"{local}@{'.'.join(labels[:-1])}"


def extract_emails(text: str) -> list[str]:
    """Find and clean every email-shaped value in a string."""
    prepared = prepare_text(text)
    found: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE_RE.finditer(prepared):
        cleaned = normalize_email(match.group(0))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    return found


def decode_cloudflare_email(encoded: str) -> str | None:
    """Decode Cloudflare's data-cfemail hex (published address, XOR-masked)."""
    hex_str = re.sub(r"[^0-9a-fA-F]", "", encoded or "")
    if len(hex_str) < 4 or len(hex_str) % 2:
        return None
    try:
        key = int(hex_str[:2], 16)
        chars = [
            chr(int(hex_str[i : i + 2], 16) ^ key)
            for i in range(2, len(hex_str), 2)
        ]
    except ValueError:
        return None
    return normalize_email("".join(chars))


def is_low_value(email: str) -> bool:
    local = email.split("@", 1)[0]
    if local in LOW_VALUE_LOCALS:
        return True
    return (
        local.startswith("noreply")
        or local.startswith("no-reply")
        or local.startswith("do-not-reply")
    )


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip().rstrip(".")
    if "@" in host:
        host = host.split("@", 1)[1]
    if host.startswith("www."):
        host = host[4:]
    if ":" in host:
        host = host.split(":", 1)[0]
    parts = [part for part in host.split(".") if part]
    if len(parts) < 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in MULTI_PART_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


def domain_affinity(email: str, hosts: list[str]) -> str:
    """exact: same site domain. brand: site name appears in the email domain."""
    domain = email.split("@", 1)[1]
    email_root = registrable_domain(domain)
    email_brand = email_root.split(".")[0]
    for host in hosts:
        if not host:
            continue
        site = registrable_domain(host)
        if email_root == site:
            return "exact"
        site_brand = site.split(".")[0]
        if len(site_brand) >= 5 and site_brand in domain:
            return "brand"
        if len(email_brand) >= 5 and email_brand in site:
            return "brand"
    return "other"


def score_email(email: str, source: str, hosts: list[str]) -> tuple[int, bool]:
    local = email.split("@", 1)[0]
    low = is_low_value(email)
    score = SOURCE_WEIGHT.get(source, 20)
    score += {"exact": 36, "brand": 18, "other": 0}[domain_affinity(email, hosts)]
    if local in ROLE_BONUS_LOCALS:
        score += 6
    if local in SOFT_PENALTY_LOCALS:
        score -= 15
    if low:
        score -= 45
    return score, low