#!/usr/bin/env python3
"""Fetcher del diario personal.

Lee feeds.yaml, baja todas las fuentes (RSS, PubMed, Google News),
deduplica, ordena por fecha y escribe data/articles.json.
Un feed caído nunca rompe la corrida: queda anotado en _errors.
"""

import base64
import hashlib
import html as htmllib
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MiDiarioPersonal/1.0"
FETCH_TIMEOUT = 20
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data)
        return data


def normalize_title(title):
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def item_id(link):
    return hashlib.sha1(link.encode()).hexdigest()[:12]


def parse_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def decode_google_news_link(link):
    """Los links de Google News RSS encapsulan el URL real en base64 (formato CBMi...).
    Si no se puede decodificar, se devuelve el link de Google (redirige en el navegador)."""
    m = re.search(r"/articles/([^?/]+)", link)
    if not m:
        return link
    token = m.group(1)
    try:
        pad = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(pad)
        urls = re.findall(rb"https?://[^\x00-\x1f\x7f-\xff]+", raw)
        for u in urls:
            u = u.decode(errors="ignore")
            if "google.com" not in u:
                return u
    except Exception:
        pass
    return link


def extract_copete(entry, title, max_len=240):
    """Bajada de la nota a partir del summary/description del feed, limpia de HTML."""
    raw = entry.get("summary") or entry.get("description") or ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"\s+", " ", htmllib.unescape(txt)).strip()
    if len(txt) < 30 or normalize_title(txt) == normalize_title(title):
        return None
    if len(txt) > max_len:
        txt = txt[:max_len].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return txt


def fetch_rss(feed_cfg):
    data = http_get(feed_cfg["url"])
    parsed = feedparser.parse(data)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feed ilegible: {parsed.bozo_exception}")
    items = []
    for entry in parsed.entries:
        link = entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        item = {
            "title": title,
            "link": link,
            "source": feed_cfg["name"],
            "published": (parse_datetime(entry) or datetime.now(timezone.utc)).isoformat(),
        }
        if feed_cfg.get("copete", True):
            copete = extract_copete(entry, title)
            if copete:
                item["copete"] = copete
        items.append(item)
    return items


def fetch_googlenews(feed_cfg):
    query = urllib.parse.quote_plus(feed_cfg["query"])
    locale = feed_cfg.get("locale", "ar")
    params = {"ar": "hl=es-419&gl=AR&ceid=AR:es-419", "us": "hl=en-US&gl=US&ceid=US:en"}[locale]
    url = f"https://news.google.com/rss/search?q={query}&{params}"
    items = fetch_rss({"name": feed_cfg["name"], "url": url, "copete": False})  # el summary de Google News es basura de links
    for it in items:
        it["link"] = decode_google_news_link(it["link"])
        # Google News pone "Titular - Medio"; separamos el medio como fuente
        m = re.match(r"^(.*)\s+-\s+([^-]{2,40})$", it["title"])
        if m:
            it["title"], it["source"] = m.group(1).strip(), m.group(2).strip()
        if it["source"].lower().endswith(".com"):
            it["source"] = it["source"][:-4]
    # descartar entradas sin título real (portadas, páginas de registro)
    return [it for it in items if len(it["title"]) >= 15]


def fetch_pubmed(feed_cfg, max_age_days):
    term = urllib.parse.quote(feed_cfg["term"])
    search_url = (f"{EUTILS}/esearch.fcgi?db=pubmed&term={term}"
                  f"&reldate={max_age_days}&datetype=edat&retmax=12&sort=date&retmode=json")
    pmids = json.loads(http_get(search_url))["esearchresult"].get("idlist", [])
    if not pmids:
        return []
    summary_url = f"{EUTILS}/esummary.fcgi?db=pubmed&id={','.join(pmids)}&retmode=json"
    result = json.loads(http_get(summary_url))["result"]
    items = []
    for pmid in pmids:
        doc = result.get(pmid)
        if not doc or not doc.get("title"):
            continue
        try:
            pub = datetime.strptime(doc.get("sortpubdate", ""), "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            pub = datetime.now(timezone.utc)
        title = re.sub(r"</?[^>]+>", "", doc["title"]).rstrip(".")
        authors = doc.get("authors") or []
        first_author = authors[0].get("name", "") if authors else ""
        journal = doc.get("fulljournalname") or doc.get("source") or ""
        copete = " · ".join(p for p in (f"{first_author} et al." if first_author else "", journal) if p)
        item = {
            "title": title,
            "link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "source": feed_cfg["name"].replace(" (PubMed)", ""),
            "published": pub.isoformat(),
        }
        if copete:
            item["copete"] = copete
        items.append(item)
    return items


def build_section(section, errors):
    cutoff = datetime.now(timezone.utc) - timedelta(days=section.get("max_age_days", 7))
    collected = []
    for feed_cfg in section["feeds"]:
        try:
            kind = feed_cfg.get("type", "rss")
            if kind == "pubmed":
                items = fetch_pubmed(feed_cfg, section.get("max_age_days", 21))
            elif kind == "googlenews":
                items = fetch_googlenews(feed_cfg)
            else:
                items = fetch_rss(feed_cfg)
        except Exception as exc:
            errors.append({"section": section["id"], "feed": feed_cfg["name"], "error": str(exc)[:200]})
            continue
        excluded = feed_cfg.get("exclude", [])
        items = [it for it in items if not any(pat in it["link"] for pat in excluded)]
        fresh = [it for it in items if datetime.fromisoformat(it["published"]) >= cutoff]
        fresh.sort(key=lambda it: it["published"], reverse=True)
        collected.extend(fresh[: feed_cfg.get("max", 5)])

    # dedupe por link y por título normalizado (Google News vs feed directo)
    seen_links, seen_titles, unique = set(), set(), []
    for it in sorted(collected, key=lambda it: it["published"], reverse=True):
        norm = normalize_title(it["title"])
        if it["link"] in seen_links or (norm and norm in seen_titles):
            continue
        seen_links.add(it["link"])
        seen_titles.add(norm)
        it["id"] = item_id(it["link"])
        unique.append(it)

    return {
        "id": section["id"],
        "title": section["title"],
        "mode": section["mode"],
        "items": unique[: section.get("max_items", 12)],
    }


def main():
    config = yaml.safe_load((ROOT / "feeds.yaml").read_text())
    errors = []
    sections = [build_section(s, errors) for s in config["sections"]]
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections": sections,
        "_errors": errors,
    }
    out_path = ROOT / "data" / "articles.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=1))

    total = sum(len(s["items"]) for s in sections)
    empty = [s["id"] for s in sections if not s["items"]]
    print(f"OK: {total} artículos en {len(sections)} secciones → {out_path}")
    for err in errors:
        print(f"  AVISO feed caído: [{err['section']}] {err['feed']}: {err['error']}", file=sys.stderr)
    if empty:
        print(f"  ERROR: secciones vacías: {empty}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
