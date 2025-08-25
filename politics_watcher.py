#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Politics Watcher — flags when a known Republican is quoted with variants of:
"I'm not racist but ..."
- Pulls per-person Google News RSS + a large set of politics feeds (and optional feeds.txt)
- Follows links and scans page text (lightweight HTML strip) for phrase variants
- De-dupes via sqlite
- Sends an email with link + snippet for each new hit
- Silent unless a match occurs
- NEW: FORCE_TEST mode to send a test alert on demand

Env (set in GitHub Actions Secrets or your local env):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO

Optional env:
  FORCE_TEST=1           -> send a single test alert email and exit(0)
  FEEDS_FILE=feeds.txt   -> extra feeds (one URL per line, # comments allowed)
  MAX_LINKS=140          -> cap number of article pages to fetch (default 120)
  TIMEOUT=20             -> per HTTP request (seconds)
"""

import os, re, sys, time, json, sqlite3, hashlib, html, urllib.request, xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse
from datetime import datetime, timezone

DB = "pol_watch.db"
OUT_DIR = "out"

TIMEOUT = int(os.environ.get("TIMEOUT", "20"))
UA = {"User-Agent": "PoliticsWatcher/1.1 (+news scanner)"}

# Broadened phrase variants (smart quotes, em dash, punctuation, spacing)
PHRASE_RE = re.compile(
    r"""
    \b( i['’]?m | i \s+ am )       # I'm / I’m / I am
    \s+ not \s+ racist \s*         # not racist
    [\s,;:\-\u2014]*               # optional punctuation/dash
    but \b                          # but
    """,
    re.IGNORECASE | re.VERBOSE
)

# Names list (one per line) comes from republicans.txt if present
def load_names(path="republicans.txt"):
    if not os.path.isfile(path):
        return [
            "Mitch McConnell","Mike Johnson","Steve Scalise","Elise Stefanik","Jim Jordan",
            "Ted Cruz","Marco Rubio","Lindsey Graham","Rand Paul","John Cornyn",
            "Josh Hawley","Tom Cotton","Mitt Romney","Marsha Blackburn","Rick Scott",
            "Ron DeSantis","Nikki Haley","Greg Abbott","Kristi Noem","Sarah Huckabee Sanders",
            "Glenn Youngkin","Brian Kemp","Doug Burgum","John Thune","John Barrasso",
            "Cynthia Lummis","Kevin Cramer","John Hoeven","Mike Lee","Joni Ernst",
        ]
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

def google_news_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"

# Built-in general feeds (we’ll merge with feeds.txt)
BASE_FEEDS = [
    # Wires / major US politics feeds
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://rss.politico.com/politics-news.xml",
    "https://thehill.com/feed/",
    "https://www.npr.org/rss/rss.php?id=1014",
    "https://rss.cnn.com/rss/cnn_allpolitics.rss",
    "https://feeds.foxnews.com/foxnews/politics",
    "https://www.abc.net.au/news/feed/51120/rss.xml",  # world/US politics pick-ups
    "https://feeds.a.dj.com/rss/RSSUSPOLITICS.xml",    # WSJ politics (often paywalled)
    "https://www.cbsnews.com/latest/rss/politics",
    "https://feeds.nbcnews.com/nbcnews/public/politics",
    "https://www.axios.com/feeds/politics.xml",
    "https://www.theguardian.com/us-news/rss",
    # State-level roundups (sample, you can add many more via feeds.txt)
    "https://apnews.com/hub/politics?output=atom",
]

def load_extra_feeds(path=os.environ.get("FEEDS_FILE", "feeds.txt")):
    feeds = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                u = ln.strip()
                if not u or u.startswith("#"):
                    continue
                feeds.append(u)
    return feeds

def fetch(url: str) -> bytes:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except Exception as e:
        print(f"[warn] fetch failed {url}: {e}", file=sys.stderr)
        return b""

def parse_rss(xml_bytes: bytes, source: str):
    items = []
    if not xml_bytes:
        return items
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    def get_ts(ts):
        for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S %Z",
                    "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S%z"):
            try:
                t = datetime.strptime(ts, fmt).astimezone(timezone.utc)
                return int(t.timestamp())
            except: pass
        return int(time.time())

    if root.tag.lower().endswith("rss"):
        ch = root.find("channel")
        if ch is None: return items
        for it in ch.findall("item"):
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link") or "").strip()
            pub   = (it.findtext("pubDate") or "").strip()
            items.append({"title": title, "url": link, "ts": get_ts(pub), "feed": source})
        return items

    if root.tag.endswith("feed") or root.tag.endswith("}feed"):
        for it in root.findall("atom:entry", ns) + root.findall("entry"):
            title = (it.findtext("atom:title", default="", namespaces=ns) or it.findtext("title", default="")).strip()
            link_el = it.find("atom:link", ns) or it.find("link")
            link = link_el.get("href") if link_el is not None else ""
            pub = (it.findtext("atom:updated", default="", namespaces=ns)
                   or it.findtext("updated", default="")
                   or it.findtext("atom:published", default="", namespaces=ns)
                   or it.findtext("published", default=""))
            items.append({"title": title, "url": link, "ts": get_ts(pub), "feed": source})
        return items

    return items

def strip_html(html_bytes: bytes) -> str:
    txt = html_bytes.decode("utf-8", errors="ignore")
    txt = re.sub(r"(?is)<script.*?>.*?</script>", " ", txt)
    txt = re.sub(r"(?is)<style.*?>.*?</style>", " ", txt)
    txt = re.sub(r"(?is)<!--.*?-->", " ", txt)
    txt = re.sub(r"(?is)<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

def fetch_text(url: str) -> str:
    raw = fetch(url)
    return strip_html(raw) if raw else ""

def ensure_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS hits(
        id TEXT PRIMARY KEY,
        ts INTEGER,
        person TEXT,
        url TEXT,
        title TEXT,
        feed TEXT,
        snippet TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS seen(
        fp TEXT PRIMARY KEY,
        ts INTEGER
    )""")
    con.commit(); con.close()

def fp(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:28]

def host(u: str) -> str:
    try: return urlparse(u).netloc
    except: return ""

def context_snippet(text: str, match: re.Match, radius=140) -> str:
    a, b = match.span()
    start = max(0, a - radius)
    end = min(len(text), b + radius)
    snippet = text[start:end].strip()
    return snippet

def record_hit(hit):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO hits(id, ts, person, url, title, feed, snippet) VALUES (?,?,?,?,?,?,?)",
                (hit["id"], hit["ts"], hit["person"], hit["url"], hit["title"], hit["feed"], hit["snippet"]))
    con.commit(); con.close()

def already_seen(key: str) -> bool:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE fp=?", (key,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO seen(fp, ts) VALUES (?,?)", (key, int(time.time())))
        con.commit()
    con.close()
    return bool(row)

def send_email(subject: str, body: str):
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("EMAIL_FROM", user)
    to_addr   = os.environ.get("EMAIL_TO", "")

    if not (host and port and user and pwd and from_addr and to_addr):
        print("[warn] SMTP/Email env not fully configured; skipping email.", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    return True

def run_once():
    ensure_db()

    # FORCE TEST mode: send a one-off test alert and exit
    if os.environ.get("FORCE_TEST", "").strip() == "1":
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        body = (
            "Politics Watcher — FORCE_TEST alert\n\n"
            f"Time:   {now}\n"
            "Person: (test)\n"
            "Title:  (test)\n"
            "Link:   https://example.com/test\n"
            "Feed:   (forced)\n\n"
            "Context snippet:\n…This is a forced test alert to confirm email wiring…\n"
        )
        ok = send_email("[Politics Watch] TEST ALERT — email wiring OK", body)
        print("[ok] Force test email sent." if ok else "[warn] Force test email skipped/failed.")
        return

    people = load_names()

    # Build per-person Google News queries for exact phrase (best precision)
    search_feeds = []
    for person in people:
        q = f"\"{person}\" AND (\"I'm not racist but\" OR \"I am not racist but\")"
        search_feeds.append((person, google_news_rss(q)))

    # Merge built-in + extra feeds
    FEEDS = BASE_FEEDS + load_extra_feeds()

    # Pull general feeds
    general_items = []
    for f in FEEDS:
        general_items.extend(parse_rss(fetch(f), f))

    # Limit page fetches per run so we don't hammer sites
    MAX_LINKS = int(os.environ.get("MAX_LINKS", "120"))
    fetched_links = 0

    new_hits = []

    # Pass 1: Targeted search feeds
    for person, rss in search_feeds:
        items = parse_rss(fetch(rss), rss)
        for it in items:
            if fetched_links >= MAX_LINKS:
                break
            key = fp(person, it["url"], it["title"])
            if already_seen(key):
                continue
            page = fetch_text(it["url"]); fetched_links += 1
            m = PHRASE_RE.search(page)
            if not m:
                continue
            snip = context_snippet(page, m)
            hit = {
                "id": fp("H", person, it["url"]),
                "ts": it["ts"],
                "person": person,
                "url": it["url"],
                "title": it["title"],
                "feed": it["feed"],
                "snippet": snip
            }
            record_hit(hit)
            new_hits.append(hit)
        if fetched_links >= MAX_LINKS:
            break

    # Pass 2: General feeds backstop
    if fetched_links < MAX_LINKS:
        for it in general_items:
            if fetched_links >= MAX_LINKS:
                break
            lower_title = it["title"].lower()
            mentions = [p for p in people if p.lower() in lower_title]
            if not mentions:
                continue
            key = fp("G", it["url"], it["title"])
            if already_seen(key):
                continue
            page = fetch_text(it["url"]); fetched_links += 1
            m = PHRASE_RE.search(page)
            if not m:
                continue
            snip = context_snippet(page, m)
            for person in mentions:
                hit = {
                    "id": fp("H", person, it["url"]),
                    "ts": it["ts"],
                    "person": person,
                    "url": it["url"],
                    "title": it["title"],
                    "feed": it["feed"],
                    "snippet": snip
                }
                record_hit(hit)
                new_hits.append(hit)

    # Email per hit
    for h in new_hits:
        subj = f"[Politics Watch] {h['person']} — phrase detected ({host(h['url'])})"
        body = (
            f"Person: {h['person']}\n"
            f"Title:  {h['title']}\n"
            f"Link:   {h['url']}\n"
            f"Feed:   {h['feed']}\n"
            f"Time:   {datetime.utcfromtimestamp(h['ts']).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Context snippet:\n…{h['snippet']}…\n"
        )
        try:
            send_email(subj, body)
        except Exception as e:
            print(f"[warn] email failed: {e}", file=sys.stderr)

    # Optional: write a tiny log file
    if new_hits:
        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(OUT_DIR, f"hits_{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump(new_hits, f, ensure_ascii=False, indent=2)

    print(f"[ok] New hits: {len(new_hits)}" if new_hits else "[ok] No matches; staying silent.")

if __name__ == "__main__":
    run_once()


