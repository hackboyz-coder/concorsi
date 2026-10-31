"""Bandi da inPA (portale ufficiale del reclutamento della PA).

Usa l'API pubblica, senza chiave, che il motore di ricerca di inPA usa per l'elenco
dei bandi: si scaricano i bandi aperti, si tengono quelli con sede a Milano (o in
provincia) e si classifica il titolo di studio richiesto.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

log = logging.getLogger("concorsi")

API_URL = os.environ.get(
    "INPA_API_URL",
    "https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better",
)
DETAIL_URL = "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id={id}"
SOURCE_URL = "https://www.inpa.gov.it/bandi-e-avvisi/"
PAGE_SIZE = int(os.environ.get("INPA_PAGE_SIZE", "100"))
MAX_PAGES = int(os.environ.get("INPA_MAX_PAGES", "80"))
DELAY = float(os.environ.get("REQUEST_DELAY", "0.8"))
# Parole che identificano la zona (minuscolo). Default: Milano città e provincia.
PLACE_WORDS = [w.strip().lower() for w in
               os.environ.get("PLACE_WORDS", "milano").split(",") if w.strip()]
PROVINCE_CODES = [w.strip().upper() for w in
                  os.environ.get("PROVINCE_CODES", "MI").split(",") if w.strip()]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.inpa.gov.it",
    "Referer": "https://www.inpa.gov.it/",
}

# Corpi di richiesta provati in ordine: il primo è quello del portale, gli altri
# servono se l'API cambia o rifiuta qualche campo.
PAYLOADS = [
    {"text": "", "categoriaId": None, "regioneId": None, "status": ["OPEN"],
     "settoreId": None, "provinciaCodice": None, "dateFrom": None, "dateTo": None,
     "livelloId": None, "salaryMin": None, "salaryMax": None, "enteRiferimentoName": ""},
    {"text": "", "status": ["OPEN"]},
    {"text": ""},
]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_sessions: dict = {}


def _session(kind):
    if kind not in _sessions:
        s = (cffi_requests.Session(impersonate="chrome") if kind == "cffi"
             else requests.Session())
        s.headers.update(HEADERS)
        _sessions[kind] = s
    return _sessions[kind]


def _post(page: int, payload: dict) -> dict:
    url = f"{API_URL}?page={page}&size={PAGE_SIZE}"
    errors = []
    for attempt in range(3):
        for kind in ["requests"] + (["cffi"] if cffi_requests else []):
            try:
                time.sleep(DELAY)
                r = _session(kind).post(url, data=json.dumps(payload), timeout=40)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]}")
                return r.json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{kind}: {e}")
                if "HTTP 400" in str(e):  # richiesta rifiutata: inutile riprovare
                    raise RuntimeError(errors[-1]) from None
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(" | ".join(errors[-2:]))


def _records(data) -> list[dict]:
    if isinstance(data, list):
        return data
    for k in ("content", "items", "results", "data", "concorsi"):
        if isinstance(data.get(k), list):
            return data[k]
    return []


def fetch_open(max_pages: int = MAX_PAGES):
    """Tutti i bandi aperti. Ritorna (record, report)."""
    report = {"pages": 0, "errors": [], "api_total": None, "payload": None}
    payload = None
    first = None
    for p in PAYLOADS:
        try:
            first = _post(0, p)
            payload = p
            break
        except RuntimeError as e:
            report["errors"].append(str(e)[:300])
    if first is None:
        return [], report
    report["payload"] = list(payload)
    report["errors"] = []  # i tentativi rifiutati prima di quello buono non contano
    out = _records(first)
    report["pages"] = 1
    total_pages = first.get("totalPages") if isinstance(first, dict) else None
    report["api_total"] = first.get("totalElements") if isinstance(first, dict) else None
    page = 1
    while out and page < max_pages and (total_pages is None or page < total_pages):
        try:
            recs = _records(_post(page, payload))
        except RuntimeError as e:
            report["errors"].append(str(e)[:300])
            break
        if not recs:
            break
        out += recs
        page += 1
        report["pages"] = page
    # se l'API non filtra lo stato, togliamo i bandi chiusi
    out = [r for r in out if not _is_closed(r)]
    return out, report


# ---------------------------------------------------------------------------
# Lettura dei campi (tollerante ai nomi)
# ---------------------------------------------------------------------------
def _strings(obj, depth=0):
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, depth + 1)


def _first(rec: dict, *keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, "", []):
            return v
    return None


def _is_closed(rec: dict) -> bool:
    st = str(_first(rec, "calculatedStatus", "status", "stato") or "").upper()
    return st in {"CLOSED", "CHIUSO", "EXPIRED", "SCADUTO"}


def _plain(text) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return " ".join(text.split())


def normalise(rec: dict) -> dict:
    rid = str(_first(rec, "id", "concorsoId", "codice") or "")
    title = _plain(_first(rec, "titolo", "title", "nome", "descrizioneBreve") or "(senza titolo)")
    desc = _plain(_first(rec, "descrizione", "descrizioneBreve", "description") or "")
    figura = _plain(_first(rec, "figuraRicercata", "profilo") or "")
    enti = _first(rec, "entiRiferimento", "enteRiferimento", "ente", "enti")
    ente = ", ".join(_plain(s) for s in _strings(enti) if len(s) > 3 and not s.isdigit())[:200] \
        if enti else ""
    deadline = _first(rec, "dataScadenza", "dataChiusura", "dataFineCandidatura", "scadenza")
    url = _first(rec, "link", "url") or DETAIL_URL.format(id=rid)
    sedi = _first(rec, "sedi", "sede", "luoghi", "localita")
    return {
        "id": rid, "url": url, "title": title, "figura": figura, "ente": ente,
        "description": desc, "deadline": str(deadline)[:10] if deadline else None,
        "posts": _first(rec, "numPosti", "numeroPosti", "posti"),
        "sedi": sedi, "raw": rec,
    }


def in_area(n: dict) -> bool:
    """Bando con sede a Milano/provincia. Se mancano le sedi si guarda tutto il record."""
    where = n["sedi"] if n["sedi"] else n["raw"]
    words = [s.lower() for s in _strings(where)]
    for s in words:
        if any(re.search(rf"\b{re.escape(w)}\b", s) for w in PLACE_WORDS):
            return True
    if n["sedi"]:
        codes = {s.strip().upper() for s in _strings(n["sedi"]) if len(s.strip()) == 2}
        if codes & set(PROVINCE_CODES):
            return True
    return False


# ---------------------------------------------------------------------------
# Titolo di studio
# ---------------------------------------------------------------------------
LOW_PATTERNS = [
    r"licenza (di )?(scuola )?media", r"scuola dell'?\s?obbligo",
    r"assolviment\w+ dell'?\s?obbligo",
    r"diploma di (scuola )?secondaria di (primo|secondo|1°|2°|i|ii) grado",
    r"diploma di istruzione secondaria", r"(scuola|istruzione) (media )?superiore",
    r"diploma (di )?(scuola )?superiore", r"diploma di maturit[aà]", r"\bmaturit[aà]\b",
    r"diploma quinquennale", r"diploma di (perito|geometra|ragioneria|ragioniere)",
    r"qualifica professionale", r"attestato di qualifica", r"\bdiplomat[io]\b",
]
LOW_RE = re.compile("|".join(LOW_PATTERNS), re.I)

# Profili che per contratto richiedono al massimo il diploma
LOW_PROFILE_RE = re.compile(
    r"\b(istruttor[ei](?! direttiv)|area (degli )?istruttori|area (degli )?operatori"
    r"|operatori? (esperto|esperti|ecologic\w*|tecnic\w*|amministrativ\w*|socio[- ]sanitari\w*)"
    r"|o\.?s\.?s\.?\b|operatore socio|esecutor[ei]|autist[ai]|cuoc[oh]i?|bidell[oi]"
    r"|collaborator[ei] scolastic\w*|ausiliari\w*|commess[oi]|usciere|centralinist\w*"
    r"|agent[ei] (di )?polizia (locale|municipale)|agent[ei] della polizia locale"
    r"|vigil[ei] (urban\w*|del fuoco)|assistent[ei] amministrativ\w*"
    r"|assistent[ei] tecnic\w*|area (degli )?assistenti|cantonier[ei]|giardinier[ei]"
    r"|magazzinier[ei]|manutentor[ei]|operai[oi]?|messo|messi notificatori"
    r"|categoria [bc]\b|cat\.? ?[bc][1-6]?\b|livello [bc]\b)",
    re.I,
)
# Segnali che il bando chiede la laurea (o più)
HIGH_RE = re.compile(
    r"\b(funzionari[oe]?|area (dei )?funzionari|elevat[ae] (qualificazion|professionalit)\w*"
    r"|dirigent[ei]|dirigenza|istruttor[ei] direttiv\w*|medic[oi]|infermier[ei]"
    r"|ingegner[ei]|architett[oi]|avvocat[oi]|psicolog[oi]|farmacist[ai]|veterinari[oi]"
    r"|biolog[oi]|chimic[oi]|fisioterapist[ai]|ostetric[hi]e|logopedist[ai]"
    r"|tecnic[oi] (sanitari\w*|della prevenzione|di radiologia|di laboratorio)"
    r"|collaborator[ei] professional[ei]|assistent[ei] social[ei]|educator[ei]"
    r"|docent[ei]|ricercator[ei]|tecnolog[oi]|professor[ei]|dottorat\w*|specializzazion\w*"
    r"|categoria d\b|cat\.? ?d[1-6]?\b|segretari[oi] comunal\w*)",
    re.I,
)
LAUREA_RE = re.compile(r"\blaurea\b|\blaureat[io]\b", re.I)


def _ctx(text, m, pad=70):
    return " ".join(text[max(0, m.start() - pad):m.end() + pad].split())


def classify(n: dict) -> tuple[str, str]:
    """Ritorna ('si' | 'no' | 'forse', motivo)."""
    text = " ".join(x for x in (n["title"], n["figura"], n["description"]) if x)
    text = text.replace("’", "'")
    no_laurea = re.sub(r"diplom\w* (di )?laurea|diploma (accademico|universitario)", " ",
                       text, flags=re.I)
    low = LOW_RE.search(no_laurea)
    if low:
        return "si", _ctx(no_laurea, low)
    head = " ".join(x for x in (n["title"], n["figura"]) if x)
    high = HIGH_RE.search(head) or LAUREA_RE.search(head)
    prof = LOW_PROFILE_RE.search(head)
    if prof and not high:
        return "si", f"profilo: {prof.group(0)}"
    if high:
        return "no", f"profilo: {high.group(0)}"
    lau = LAUREA_RE.search(text)
    if lau:
        return "no", _ctx(text, lau)
    prof = LOW_PROFILE_RE.search(text)
    if prof and not HIGH_RE.search(text):
        return "si", f"profilo: {prof.group(0)}"
    return "forse", "titolo di studio non indicato nell'annuncio"


# ---------------------------------------------------------------------------
# Esclusioni: bandi da non segnalare mai (parole nel titolo o nel profilo)
# ---------------------------------------------------------------------------
EXCLUDE = [w.strip() for w in os.environ.get(
    "EXCLUDE_WORDS", "avviso di mobilità,istruttore").split(",") if w.strip()]


def _fold(text: str) -> str:
    """Minuscolo e senza accenti, per confronti tolleranti."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return t.lower().replace("'", " ")


def _exclude_re(phrase: str):
    # ogni parola lunga accetta qualsiasi finale: istruttore -> istruttori, istruttrice no
    parts = []
    for w in _fold(phrase).split():
        parts.append(re.escape(w[:-1]) + r"\w*" if len(w) > 4 else re.escape(w))
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b")


EXCLUDE_RES = [(p, _exclude_re(p)) for p in EXCLUDE]


def excluded(n: dict) -> str | None:
    text = _fold(" ".join(x for x in (n["title"], n["figura"]) if x))
    for phrase, rx in EXCLUDE_RES:
        if rx.search(text):
            return phrase
    return None


def fetch_milano():
    """Bandi aperti nella zona, già classificati. Ritorna (lista, report)."""
    recs, report = fetch_open()
    out = []
    for r in recs:
        n = normalise(r)
        if not n["id"] or not in_area(n):
            continue
        ex = excluded(n)
        if ex:
            n["class"], n["reason"] = "no", f"escluso: contiene «{ex}»"
        else:
            n["class"], n["reason"] = classify(n)
        n.pop("raw")
        out.append(n)
    report["open_total"] = len(recs)
    return out, report
