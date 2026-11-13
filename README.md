Contact crawler

Extracts public emails, phone numbers, and postal addresses from websites.

It reads the page HTML only (no forms, no logins). Sources include mailto: / tel: links, visible text, meta descriptions, JSON-LD, and Cloudflare data-cfemail. When the homepage has no usable business email, it follows contact-like links (Contact, Get in touch, Offices, …). If those are missing, it tries common paths such as /contact and /contact-us.

There is no per-site hardcoded contact list. Values always come from the live page.

Requirements





Python 3.10+



No pip packages (requirements.txt is empty on purpose; only the standard library is used)



Optional: curl (fallback for hosts that only answer over HTTP/2)



Optional: a Zyte Smart Proxy API key (used only when a direct fetch is blocked or returns an empty page)

Setup

git clone <repo-url>
cd <repo-folder>

Copy the env template and add a Zyte key if you have one:

# macOS / Linux
cp .env.example .env

# Windows (PowerShell or Command Prompt)
copy .env.example .env

Edit .env:

ZYTE_API_KEY=your-key-here
ZYTE_PROXY_HOST=api.zyte.com
ZYTE_PROXY_PORT=8011

.env is listed in .gitignore and is not committed. Without a key the crawler still runs; blocked sites stay blocked because the proxy retry is skipped.

The Zyte CA certificate ships with the repo at contact_crawler/zyte-ca.crt.

Run

From the folder that contains run.py and domains.txt:

# Crawl the sample domains and write JSON + CSV
python -m contact_crawler domains.txt -o results.json --csv results.csv

# One site
python -m contact_crawler https://abbeyrun.com

# Faster / slower concurrency (optional)
python -m contact_crawler domains.txt -o results.json --csv results.csv --workers 10 --rate 10

Progress lines go to stderr. The JSON array is printed to stdout and, with -o, also saved to a file.

Useful flags







Flag



Default



Meaning





--workers



10



How many sites crawl at the same time





--rate



10



Max HTTP requests started per second across all workers (0 = no cap)





--timeout



15



Per-request timeout in seconds





--max-contact-pages



4



Extra pages to open when the homepage has no usable email





--no-probe



off



Do not guess /contact paths; only follow links found in HTML

domains.txt is the sample list used while building this. Lines starting with # are ignored.

How fetching works





Fetch the homepage directly (https, then http / www variants as needed).



If the response is a bot wall (e.g. Cloudflare) or the connection returns no page, retry once through Zyte (only when ZYTE_API_KEY is set).



Follow a meta refresh if present.



Parse emails / phones / addresses from the page.



If there is still no usable email, open ranked contact links, then optional path probes.



Clean and score emails; pick best_email.

A successful proxy retry is noted as fetched_via_zyte_proxy in the result.

Email cleanup

These all become bob@abbeyrun.com:





bob@abbeyrun.com



bob at abbeyrun dot com



bob [at] abbeyrun [dot] com



bob(at)abbeyrun(dot)com



info@company dot com



info @ company . com



booking@acemolar.com

info at company dot co dot uk becomes info@company.co.uk.

Dropped on purpose:





image names such as Ali@1.2x.png



placeholders such as user@domain.com



platform addresses (sentry.io, wixpress.com, and similar)

Each kept address has a score. A mailto: on the site’s own domain ranks above a loose text match on another domain. noreply@ is kept but flagged low_value so a real inbox is preferred.

Result fields







Field



Meaning





status



ok, blocked, unavailable, error, empty, or unreachable





best_email



Highest-scoring address that is not noreply





emails



Cleaned addresses with source (mailto, text, jsonld, meta, cfemail), page, and score





phones



From tel: links, JSON-LD, then visible text





addresses



JSON-LD postal addresses, plus street lines that include a postal code





contact_links



Contact-like URLs discovered on the pages that were read





pages_fetched



Homepage (after any meta refresh) and contact pages actually opened





notes



e.g. redirected_to:…, meta_refresh:…, fetched_via_zyte_proxy, no_email_found

A domain that redirects to a different site is still read. The result notes redirected_to: and scores emails against both the name you typed and the final host.

Tests

python -m unittest discover -s tests

Project layout

domains.txt            # sample targets
requirements.txt       # empty (stdlib only)
.env.example           # copy to .env
contact_crawler/
  cli.py               # command line
  crawl.py             # one-site crawl flow
  fetch.py             # HTTP + optional Zyte fallback
  extract.py           # HTML / JSON-LD / phones / addresses
  cleaner.py           # email cleanup and scoring
  env.py               # loads .env
  zyte-ca.crt          # Zyte proxy certificate
tests/

Only crawl sites you are allowed to request.
