# Concorsi Milano · per diplomati

Web app (stessa struttura di Price Watch) che una volta al giorno legge i bandi aperti
da **inPA** (portale ufficiale del reclutamento, https://www.inpa.gov.it), tiene quelli
con sede a Milano o in provincia e invia un'email a `ALERT_EMAIL` quando il bando
richiede **al massimo il diploma di scuola secondaria superiore**: licenza media, scuola
dell'obbligo, qualifica professionale o maturità, oppure profili che per contratto
richiedono il diploma (istruttore, operatore, agente di polizia locale, assistente
amministrativo...). I bandi per laureati vengono ignorati.

I bandi il cui annuncio non indica il titolo di studio finiscono in **Da verificare**
(nessuna email, a meno di `NOTIFY_UNSURE=1`).

- Ogni bando viene segnalato una volta sola. Al primo controllo arriva un'unica email
  con tutti i bandi per diplomati già aperti; poi solo i nuovi.
- Ogni bando mostra il motivo della classificazione; se il filtro sbaglia, i pulsanti
  accanto al bando lo correggono.
- **Diagnostica** mostra cosa risponde l'API di inPA (utile se l'API cambia).

## Deploy su Render

1. **Resend:** usa la stessa API key di Price Watch (account registrato con
   `versi.traccianti6@icloud.com`), oppure creane una nuova su https://resend.com → *API Keys*.
2. Su Render: **New → Blueprint**, scegli il repository `concorsi`. Render legge `render.yaml`.
3. Quando lo chiede, inserisci:
   - `RESEND_API_KEY` → la chiave di Resend
   - `APP_PASSWORD` → una password a tua scelta per aprire la web app
     (all'accesso il nome utente può essere qualsiasi)
4. Apri `https://concorsi-milano-xxxx.onrender.com`, premi **Email di prova**, poi
   **Controlla ora**.

Costo: piano Starter (7 $/mese) + disco da 1 GB (~0,25 $/mese).

## Variabili d'ambiente

| Variabile | Uso |
|---|---|
| `ALERT_EMAIL` | destinatario degli avvisi |
| `RESEND_API_KEY` | chiave Resend (consigliato) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | alternativa SMTP |
| `APP_PASSWORD` | protegge la web app |
| `CHECK_INTERVAL_MINUTES` | frequenza dei controlli (default 1440 = 24 ore) |
| `PLACE_WORDS`, `PROVINCE_CODES` | zona (default `milano` e `MI`, separati da virgola) |
| `NOTIFY_UNSURE` | `1` = manda email anche per i bandi da verificare |
| `INPA_API_URL`, `INPA_MAX_PAGES` | endpoint e pagine massime dell'API inPA |
| `DATA_DIR` / `DATABASE_URL` | SQLite su disco oppure Postgres |
| `CRON_TOKEN` | per avviare un controllo da fuori: `/cron?token=...` |

## In locale

```bash
pip install -r requirements.txt
ALERT_EMAIL=tu@esempio.it RESEND_API_KEY=re_... python app.py
```
