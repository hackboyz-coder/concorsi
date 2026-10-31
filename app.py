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
import inpa

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
    "bandi_inpa", meta,
    Column("id", Integer, primary_key=True),
    Column("ext_id", String(100), unique=True, nullable=False),   # id inPA
    Column("url", String(500)),
    Column("title", Text),
    Column("ente", Text),
    Column("description", Text),
    Column("deadline", String(40)),
    Column("posts", String(20)),
    Column("klass", String(10)),                   # si | no | forse (automatico)
    Column("override", String(10), nullable=True),  # correzione manuale
    Column("reason", Text),
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

NOTIFY_UNSURE = os.environ.get("NOTIFY_UNSURE", "0") == "1"


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


def klass(b):
    return b["override"] or b["klass"] or "forse"


def to_notify(b):
    k = klass(b)
    return k == "si" or (k == "forse" and NOTIFY_UNSURE and not b["override"])


# ---------- scansione e avvisi ----------
def notify_pending():
    """Invia una mail con i bandi per diplomati non ancora segnalati."""
    pending = [b for b in rows(select(bandi).where(bandi.c.notified_at.is_(None),
                                                   bandi.c.present.is_(True)))
               if to_notify(b)]
    if not pending:
        return 0
    try:
        notifier.bandi_alert(pending, inpa.SOURCE_URL)
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
        found, report = inpa.fetch_milano()
        ts = now()
        known = {b["ext_id"] for b in rows(select(bandi.c.ext_id))}
        with engine.begin() as c:
            if report.get("open_total"):  # se l'API non risponde non tocchiamo l'elenco
                c.execute(update(bandi).values(present=False))
            for n in found:
                vals = dict(url=n["url"], title=n["title"], ente=n["ente"],
                            description=(n["description"] or "")[:4000],
                            deadline=n["deadline"],
                            posts=str(n["posts"]) if n["posts"] is not None else None,
                            klass=n["class"], reason=n["reason"][:300],
                            present=True, last_seen=ts)
                if n["id"] in known:
                    c.execute(update(bandi).where(bandi.c.ext_id == n["id"]).values(**vals))
                else:
                    c.execute(insert(bandi).values(ext_id=n["id"], first_seen=ts, **vals))
        sent = notify_pending() if found else 0
        ts = now()
        report.update(at=ts, seconds=int((ts - started).total_seconds()), total=len(found),
                      emailed=sent, errors=report["errors"][:10],
                      si=sum(1 for n in found if n["class"] == "si"),
                      forse=sum(1 for n in found if n["class"] == "forse"))
        set_state("last_scan", report)
        log.info("Scansione: %s aperti in Italia, %d a Milano, %d segnalati",
                 report.get("open_total"), len(found), sent)
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


VIEWS = {"si": "Per diplomati", "forse": "Da verificare", "tutti": "Tutti i bandi"}


@app.get("/")
@protected
def index():
    view = request.args.get("vista", "si")
    view = view if view in VIEWS else "si"
    items = rows(select(bandi).where(bandi.c.present.is_(True))
                 .order_by(bandi.c.first_seen.desc(), bandi.c.id.desc()))
    counts = {"si": 0, "forse": 0, "no": 0}
    for b in items:
        b["k"] = klass(b)
        counts[b["k"]] = counts.get(b["k"], 0) + 1
    shown = items if view == "tutti" else [b for b in items if b["k"] == view]
    return render_template("index.html", items=shown, total=len(items), counts=counts,
                           view=view, views=VIEWS, scan=get_state("last_scan"),
                           scanning=get_state("scanning", False), interval=interval_label(),
                           email_ok=notifier.email_configured(),
                           email_error=get_state("last_email_error"),
                           alert_email=os.environ.get("ALERT_EMAIL"),
                           notify_unsure=NOTIFY_UNSURE)


@app.post("/bandi/<int:bid>/set/<value>")
@protected
def set_class(bid, value):
    """Correzione manuale: si = per diplomati, no = non adatto."""
    if value in ("si", "no"):
        with engine.begin() as c:
            r = c.execute(select(bandi.c.notified_at).where(bandi.c.id == bid)).first()
            if r is not None:
                c.execute(update(bandi).where(bandi.c.id == bid).values(
                    override=value,
                    # una correzione manuale non fa partire una mail
                    notified_at=r[0] or now()))
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
    """Interroga inPA (3 pagine) e mostra cosa riceve l'app: utile se l'API cambia."""
    out = [f"API: {inpa.API_URL}"]
    try:
        recs, rep = inpa.fetch_open(max_pages=3)
        out.append(f"Corpo richiesta accettato: {rep['payload']}")
        out.append(f"Bandi aperti totali (API): {rep['api_total']} · letti ora: {len(recs)}")
        out += [f"Errore: {e}" for e in rep["errors"]]
        if recs:
            out.append(f"\nCampi del primo bando: {sorted(recs[0].keys())}")
            out.append("Primo bando (JSON, troncato):")
            out.append(json.dumps(recs[0], ensure_ascii=False, indent=1)[:3000])
            mil = []
            for r in recs:
                n = inpa.normalise(r)
                if inpa.in_area(n):
                    n["class"], n["reason"] = inpa.classify(n)
                    mil.append(n)
            out.append(f"\nDI CUI A MILANO in queste pagine: {len(mil)}")
            for n in mil:
                out.append(f"- [{n['class']}] {n['title']} · {n['ente']}\n  {n['url']}\n"
                           f"  motivo: {n['reason']}")
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
