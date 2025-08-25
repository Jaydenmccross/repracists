#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Politics Watcher — flags when a known Republican is quoted with variants of:
"I'm not racist but ..."
- Pulls per-person Google News RSS (public) + a few politics feeds
- Follows links and scans page text (lightweight HTML strip) for phrase variants
- De-dupes via sqlite
- Sends an email with link + snippet for each new hit
- Silent unless a match occurs

Env (set in GitHub Actions Secrets or your local env):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO
"""

import os, re, sys, time, json, sqlite3, hashlib, html, urllib.request, xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse
from datetime import datetime, timezone

DB = "pol_watch.db"
OUT_DIR = "out"  # optional logs
TIMEOUT = 20
UA = {"User-Agent": "PoliticsWatcher/1.0 (+news scanner)"}

# Phrase variants (smart quotes, punctuation, spacing, 'I'm'/'I am', elisions)
PHRASE_RE = re.compile(
    r"""
    \b( i['’]?m | i \s+ am )       # I'm / I’m / I am
    \s+ not \s+ racist \s*         # not racist
    [,;:\-\u2014]? \s*             # optional punctuation
    but \b                          # but
    """,
    re.IGNORECASE | re.VERBOSE
)

# Light “Republican” list comes from republicans.txt (one name per line).
def load_names(path="republicans.txt"):
    if not os.path.isfile(path):
        # Seed with a small starter list you can expand freely.
        return [
            "Mitch McConnell", "Kevin McCarthy", "Mike Johnson",
            "Mitt Romney", "Lindsey Graham", "Ted Cruz", "Marco Rubio",
            "Elise Stefanik", "Marjorie Taylor Greene", "Jim Jordan",
            "Ron DeSantis", "Nikki Haley", "Greg Abbott", "Sarah Huckabee Sanders",
            "John Cornyn", "Josh Hawley", "Tom Cotton", "Rand Paul"
        ]
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

def google_news_rss(query: str) -> str:
    # Public search RSS from Google News
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"

# A few general politics feeds (extra coverage)
FEEDS = [
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://feeds.a.dj.com/rss/RSSUSPOLITICS.xml",
    "https://rss.politico.com/politics-news.xml",
    "https://thehill.com/feed/",
    "https://feeds.foxnews.com/foxnews/politics",
    "https://www.npr.org/rss/rss.php?id=1014",
    "https://rss.cnn.com/rss/cnn_allpolitics.rss"
]

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
        # best-effort; fall back to now
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
    # extremely simple “text extractor”: remove tags, compress whitespace
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

def context_snippet(text: str, match: re.Match, radius=120) -> str:
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

    with smtplib.SMTP(host, port, timeout=25) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    return True

def run_once():
    ensure_db()
    people = load_names()

    # Build per-person Google News RSS queries focused on the target phrase
    # (limiting false positives by requiring the key words in title/body)
    search_feeds = []
    for person in people:
        q = f"\"{person}\" AND (\"I'm not racist but\" OR \"I am not racist but\")"
        search_feeds.append((person, google_news_rss(q)))

    # Pull general feeds once
    general_items = []
    for f in FEEDS:
        general_items.extend(parse_rss(fetch(f), f))

    new_hits = []

    # 1) Search feeds per person (most likely to find exact phrase)
    for person, rss in search_feeds:
        items = parse_rss(fetch(rss), rss)
        for it in items:
            key = fp(person, it["url"], it["title"])
            if already_seen(key):
                continue
            page = fetch_text(it["url"])
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

    # 2) Backstop: scan general feeds where title mentions a person; then fetch page
    for it in general_items:
        lower_title = it["title"].lower()
        mentions = [p for p in people if p.lower() in lower_title]
        if not mentions:
            continue
        key = fp("G", it["url"], it["title"])
        if already_seen(key):
            continue
        page = fetch_text(it["url"])
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

    # Email per hit (keeps it simple & immediate)
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

    # Optional: write a tiny log file for visibility
    if new_hits:
        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(OUT_DIR, f"hits_{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump(new_hits, f, ensure_ascii=False, indent=2)

    print(f"[ok] New hits: {len(new_hits)}" if new_hits else "[ok] No matches; staying silent.")

if __name__ == "__main__":
    run_once()
