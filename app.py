import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, update)
from sqlalchemy.exc import OperationalError

import notifier
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("concorsi")

# ---------- database ----------
db_url = os.environ.get("DATABASE_URL")
if db_url:
    for old in ("postgres://", "postgresql://"):
        if db_url.startswith(old):
            db_url = db_url.replace(old, "postgresql+psycopg://", 1)
else:
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    os.makedirs(data_dir, exist_ok=True)
    db_url = f"sqlite:///{os.path.join(data_dir, 'concorsi.db')}"

if db_url.startswith("sqlite"):
    from sqlalchemy import event

    engine = create_engine(db_url, future=True,
                           connect_args={"timeout": 30, "check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
else:
    engine = create_engine(db_url, pool_pre_ping=True, future=True)

meta = MetaData()
bandi = Table(
    "bandi", meta,
    Column("id", Integer, primary_key=True),
    Column("url", String(500), unique=True, nullable=False),
    Column("title", Text),
    Column("snippet", Text),
    Column("match", Boolean, default=False),       # per diplomati (automatico)
    Column("override", Boolean, nullable=True),    # correzione manuale
    Column("reason", Text),
    Column("deadline", String(40)),
    Column("detail_ok", Boolean, default=False),
    Column("tries", Integer, default=0),
    Column("present", Boolean, default=True),
    Column("first_seen", DateTime(timezone=True)),
    Column("last_seen", DateTime(timezone=True)),
    Column("notified_at", DateTime(timezone=True)),
)
state = Table(
    "state", meta,
    Column("key", String(50), primary_key=True),
    Column("value", Text),
)
meta.create_all(engine)


def now():
    return datetime.now(timezone.utc)


def rows(stmt):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def get_state(key, default=None):
    r = rows(select(state).where(state.c.key == key))
    return json.loads(r[0]["value"]) if r else default


def set_state(key, value):
    with engine.begin() as c:
        c.execute(delete(state).where(state.c.key == key))
        c.execute(insert(state).values(key=key, value=json.dumps(value, default=str)))


def is_match(b):
    return b["override"] if b["override"] is not None else bool(b["match"])


# ---------- scansione e avvisi ----------
def notify_pending():
    """Invia una mail con i bandi per diplomati non ancora segnalati."""
    pending = [b for b in rows(select(bandi).where(bandi.c.notified_at.is_(None),
                                                   bandi.c.present.is_(True)))
               if is_match(b)]
    if not pending:
        return 0
    try:
        notifier.bandi_alert(pending, scraper.LIST_URL)
    except Exception as e:  # noqa: BLE001
        log.error("Email non inviata: %s", e)
        set_state("last_email_error", f"{now():%d/%m %H:%M} {e}")
        return 0
    with engine.begin() as c:
        c.execute(update(bandi).where(bandi.c.id.in_([b["id"] for b in pending]))
                  .values(notified_at=now()))
    set_state("last_email_error", None)
    log.info("Email inviata: %d bandi", len(pending))
    return len(pending)


scan_lock = threading.Lock()


def scan():
    if not scan_lock.acquire(blocking=False):
        log.info("Scansione già in corso")
        return
    try:
        set_state("scanning", True)
        started = now()
        found, report = scraper.list_concorsi()
        ts = now()
        known = {b["url"]: b for b in rows(select(bandi))}
        with engine.begin() as c:
            if found:  # se il sito non risponde non tocchiamo l'elenco noto
                c.execute(update(bandi).values(present=False))
            for url, it in found.items():
                if url in known:
                    c.execute(update(bandi).where(bandi.c.url == url).values(
                        present=True, last_seen=ts, title=it["title"], snippet=it["snippet"]))
                else:
                    c.execute(insert(bandi).values(
                        url=url, title=it["title"], snippet=it["snippet"], present=True,
                        first_seen=ts, last_seen=ts, tries=0, detail_ok=False, match=False))

        # analizza le schede nuove (o non lette le volte precedenti, max 3 tentativi)
        todo = rows(select(bandi).where(bandi.c.present.is_(True), bandi.c.detail_ok.is_(False),
                                        bandi.c.tries < 3))
        analysed = 0
        for b in todo[:scraper.MAX_DETAIL_PAGES]:
            res = scraper.analyse(b)
            analysed += 1
            with engine.begin() as c:
                c.execute(update(bandi).where(bandi.c.id == b["id"]).values(
                    match=res["match"] or bool(b["match"]), reason=res["reason"] or b["reason"],
                    deadline=res["deadline"] or b["deadline"], detail_ok=res["detail_ok"],
                    tries=(b["tries"] or 0) + 1))

        sent = notify_pending() if found else 0
        ts = now()
        report.update(at=ts, seconds=int((ts - started).total_seconds()), total=len(found),
                      analysed=analysed, emailed=sent, errors=report["errors"][:10])
        set_state("last_scan", report)
        log.info("Scansione: %d bandi, %d analizzati, %d segnalati", len(found), analysed, sent)
    except Exception as e:  # noqa: BLE001
        log.exception("Scansione fallita")
        set_state("last_scan", {"at": now(), "total": 0, "fatal": str(e), "errors": []})
    finally:
        set_state("scanning", False)
        scan_lock.release()


# ---------- web ----------
app = Flask(__name__)
APP_PASSWORD = os.environ.get("APP_PASSWORD")
CRON_TOKEN = os.environ.get("CRON_TOKEN")
INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "1440"))
scheduler = None


@app.template_filter("when")
def when(dt):
    if not dt:
        return "mai"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int((now() - dt).total_seconds() // 60)
    if mins < 1:
        return "adesso"
    if mins < 60:
        return f"{mins} min fa"
    if mins < 60 * 24:
        return f"{mins // 60} h fa"
    return f"{mins // 1440} g fa"


def protected(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if APP_PASSWORD:
            auth = request.authorization
            if not auth or not secrets.compare_digest(auth.password or "", APP_PASSWORD):
                return Response("Accesso riservato", 401,
                                {"WWW-Authenticate": 'Basic realm="Concorsi Watch"'})
        return f(*a, **kw)
    return wrapper


def start_scan_async():
    if scheduler:
        scheduler.add_job(scan, id="manual_scan", replace_existing=True)
    else:
        threading.Thread(target=scan, daemon=True).start()


def interval_label():
    return f"{INTERVAL // 60} ore" if INTERVAL % 60 == 0 else f"{INTERVAL} minuti"


@app.get("/")
@protected
def index():
    show_all = request.args.get("tutti") == "1"
    items = rows(select(bandi).where(bandi.c.present.is_(True))
                 .order_by(bandi.c.first_seen.desc(), bandi.c.id.desc()))
    for b in items:
        b["ok"] = is_match(b)
    n_match = sum(1 for b in items if b["ok"])
    view = items if show_all else [b for b in items if b["ok"]]
    return render_template("index.html", items=view, total=len(items), n_match=n_match,
                           show_all=show_all, scan=get_state("last_scan"),
                           scanning=get_state("scanning", False), interval=interval_label(),
                           email_ok=notifier.email_configured(),
                           email_error=get_state("last_email_error"),
                           alert_email=os.environ.get("ALERT_EMAIL"),
                           source=scraper.LIST_URL)


@app.post("/bandi/<int:bid>/toggle")
@protected
def toggle(bid):
    """Correzione manuale: segna un bando come per diplomati o no."""
    r = rows(select(bandi).where(bandi.c.id == bid))
    if r:
        b = r[0]
        with engine.begin() as c:
            c.execute(update(bandi).where(bandi.c.id == bid).values(
                override=not is_match(b),
                # una correzione manuale non deve far partire una mail
                notified_at=b["notified_at"] or now()))
    return redirect(request.referrer or url_for("index"))


@app.post("/scan")
@protected
def scan_now():
    start_scan_async()
    return redirect(url_for("index"))


@app.post("/test-email")
@protected
def test_email():
    try:
        notifier.send_email("Concorsi Watch: email di prova",
                            "<p>Funziona! Riceverai qui gli avvisi sui concorsi di Milano.</p>")
        return jsonify(ok=True)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e))


@app.get("/diagnostica")
@protected
def diagnostica():
    """Mostra cosa legge l'app dalla prima pagina dell'elenco: utile se il sito cambia."""
    out = [f"URL elenco: {scraper.LIST_URL}"]
    try:
        html = scraper.fetch(scraper.LIST_URL)
        found, nxt, rejected = scraper.parse_listing(html, scraper.LIST_URL)
        out.append(f"HTML scaricato: {len(html)} caratteri")
        out.append(f"Pagina successiva: {nxt or 'nessuna'}")
        out.append(f"\nBANDI RICONOSCIUTI ({len(found)}):")
        for it in found.values():
            ok, reason = scraper.requires_at_most_diploma(it["snippet"])
            out.append(f"- {it['title']}\n  {it['url']}\n  dall'elenco: "
                       f"{'diploma -> ' + reason if ok else 'titolo non indicato'}")
        out.append(f"\nLINK /concorsi/ SCARTATI ({len(rejected)}):")
        out += [f"- {t} -> {u}" for t, u in rejected[:60]]
    except Exception as e:  # noqa: BLE001
        out.append(f"ERRORE: {e}")
    return Response("\n".join(out), mimetype="text/plain; charset=utf-8")


@app.get("/cron")
def cron():
    if not CRON_TOKEN or not secrets.compare_digest(request.args.get("token", ""), CRON_TOKEN):
        return "forbidden", 403
    start_scan_async()
    return "ok"


@app.errorhandler(OperationalError)
def db_busy(e):
    log.warning("Database occupato: %s", e)
    return Response("<p style='font-family:system-ui;padding:30px'>Database momentaneamente "
                    "occupato (scansione in corso). Ricarica tra qualche secondo.</p>",
                    503, {"Retry-After": "10"})


@app.get("/health")
def health():
    return "ok"


set_state("scanning", False)
if os.environ.get("DISABLE_SCHEDULER") != "1":
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(scan, "interval", minutes=INTERVAL, id="scan",
                      max_instances=1, coalesce=True,
                      next_run_time=now() + timedelta(
                          seconds=int(os.environ.get("FIRST_SCAN_DELAY", "60"))))
    scheduler.start()

if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
