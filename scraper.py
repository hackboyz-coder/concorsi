"""Lettura dei concorsi di Milano da concorsipubblici.com e classificazione
del titolo di studio richiesto.

Il sito non ha API: si legge la pagina elenco (e le successive), si prendono i
link alle schede dei bandi e, per ogni bando nuovo, si apre la scheda e si
cerca nel testo il titolo di studio.
"""
from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("concorsi")

LIST_URL = os.environ.get(
    "LIST_URL", "https://www.concorsipubblici.com/concorsi/regione/loc/milano"
)
MAX_LIST_PAGES = int(os.environ.get("MAX_LIST_PAGES", "5"))
MAX_DETAIL_PAGES = int(os.environ.get("MAX_DETAIL_PAGES", "120"))
DELAY = float(os.environ.get("REQUEST_DELAY", "1.2"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9",
}


# ---------------------------------------------------------------------------
# Classificazione: "al massimo diploma di scuola superiore"
# ---------------------------------------------------------------------------
LOW_TITLE_PATTERNS = [
    r"licenza (di )?(scuola )?media",
    r"scuola dell'?\s?obbligo",
    r"assolviment\w+ dell'?\s?obbligo",
    r"diploma di (scuola )?secondaria di (primo|1°|i) grado",
    r"diploma di (scuola )?secondaria di (secondo|2°|ii) grado",
    r"diploma di istruzione secondaria( superiore)?",
    r"(scuola|istruzione) (media )?superiore",
    r"diploma (di )?(scuola )?superiore",
    r"diploma di maturit[aà]",
    r"\bmaturit[aà]\b",
    r"diploma quinquennale",
    r"diploma di (perito|geometra|ragioneria|ragioniere)",
    r"qualifica professionale",
    r"attestato di qualifica",
    r"\bdiplomat[io]\b",
    r"nessun titolo di studio",
]
LOW_RE = re.compile("|".join(LOW_TITLE_PATTERNS), re.I)
# Campo strutturato tipo "Titolo di studio: Diploma" (ma non "Diploma di laurea")
FIELD_RE = re.compile(
    r"titolo di studio\s*[:\-]?\s*(?!.{0,15}laurea)[^\n]{0,60}"
    r"(diploma|licenza|obbligo|maturit)",
    re.I,
)


def requires_at_most_diploma(text: str) -> tuple[bool, str]:
    """(True, frase trovata) se il bando ammette chi ha al massimo il diploma."""
    t = re.sub(r"[ \t\r\f\v]+", " ", text or "").replace("’", "'")
    m = FIELD_RE.search(t)
    if m:
        return True, m.group(0).strip()
    cleaned = re.sub(r"diplom\w* (di )?laurea", " ", t, flags=re.I)
    cleaned = re.sub(r"diploma (accademico|universitario|di specializzazione)", " ",
                     cleaned, flags=re.I)
    m = LOW_RE.search(cleaned)
    if m:
        start = max(0, m.start() - 70)
        return True, " ".join(cleaned[start:m.end() + 70].split())
    return False, ""


DEADLINE_RE = re.compile(
    r"(scadenza|scade|entro (il)?)\D{0,25}(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{1,2} (gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
    r"settembre|ottobre|novembre|dicembre) \d{4})",
    re.I,
)


def find_deadline(text: str) -> str | None:
    m = DEADLINE_RE.search(text or "")
    return m.group(3) if m else None


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> str:
    last = None
    for attempt in range(3):
        try:
            time.sleep(DELAY)
            r = session.get(url, timeout=30)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def main_content(soup: BeautifulSoup):
    for sel in ["main", "article", "#content", ".content", "#main", ".main"]:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    return soup.body or soup


SKIP_PARTS = ("/regione/", "/loc/", "/provincia/", "/categoria/", "/ente/",
              "/tag/", "/page/", "login", "registr")


def is_detail_link(href: str, base_host: str) -> bool:
    p = urlparse(href)
    if p.netloc and p.netloc != base_host:
        return False
    path = p.path.lower()
    if not path.startswith("/concorsi/"):
        return False
    if any(s in path for s in SKIP_PARTS):
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(re.search(r"\d", last)) or len(last) > 25


def parse_listing(html: str, url: str):
    """Ritorna (dict url->concorso, url pagina successiva o None, link scartati)."""
    host = urlparse(url).netloc
    soup = BeautifulSoup(html, "html.parser")
    content = main_content(soup)
    found, rejected = {}, []
    for a in content.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < 15 or not is_detail_link(href, host):
            if "/concorsi/" in href:
                rejected.append((title[:80], href))
            continue
        block = a
        for _ in range(4):
            if block.parent is None:
                break
            block = block.parent
            if len(block.get_text(strip=True)) > 150:
                break
        snippet = " ".join(block.get_text(" ", strip=True).split())[:600]
        if href not in found or len(title) > len(found[href]["title"]):
            found[href] = {"url": href, "title": title, "snippet": snippet}

    nxt = (soup.find("a", rel="next")
           or soup.find("a", string=re.compile(r"^\s*(successiv|avanti|›|»|>)", re.I)))
    next_url = urljoin(url, nxt["href"]) if nxt and nxt.get("href") else None
    if next_url == url:
        next_url = None
    return found, next_url, rejected


def list_concorsi() -> tuple[dict, dict]:
    """Tutti i concorsi dell'elenco. Ritorna (concorsi, report)."""
    found, report = {}, {"pages": 0, "errors": []}
    url = LIST_URL
    for _ in range(MAX_LIST_PAGES):
        try:
            html = fetch(url)
        except RuntimeError as e:
            report["errors"].append(str(e))
            break
        page, url, _rej = parse_listing(html, url)
        report["pages"] += 1
        for k, v in page.items():
            found.setdefault(k, v)
        if not url:
            break
    return found, report


def detail_text(url: str) -> str:
    soup = BeautifulSoup(fetch(url), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    return main_content(soup).get_text("\n", strip=True)


def analyse(concorso: dict) -> dict:
    """Apre la scheda del bando e decide se è per diplomati."""
    try:
        text = detail_text(concorso["url"])
    except RuntimeError as e:
        log.warning("Scheda non letta: %s", e)
        text = ""
    ok, reason = requires_at_most_diploma(text)
    if not ok:
        ok, reason = requires_at_most_diploma(concorso.get("snippet", ""))
    return {
        "match": ok,
        "reason": reason[:300],
        "deadline": find_deadline(text) or find_deadline(concorso.get("snippet", "")),
        "detail_ok": bool(text),
    }
