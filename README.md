# Concorsi Milano · per diplomati

Web app (stessa struttura di Price Watch) che una volta al giorno legge
https://www.concorsipubblici.com/concorsi/regione/loc/milano, apre ogni bando nuovo e
invia un'email a `ALERT_EMAIL` quando il bando richiede **al massimo il diploma di
scuola secondaria superiore**: licenza media, scuola dell'obbligo, qualifica
professionale o diploma di maturità. I bandi che chiedono la laurea vengono ignorati.

- Ogni bando viene segnalato una volta sola. Al primo controllo arriva un'unica email
  con tutti i bandi per diplomati già aperti; poi solo i nuovi.
- La pagina **Per diplomati** mostra i bandi trovati con la frase del bando che ha fatto
  scattare il filtro; **Tutti i bandi** mostra anche gli altri. Se il filtro sbaglia, il
  pulsante accanto a ogni bando lo corregge.
- **Diagnostica** mostra cosa legge l'app dalla pagina del sito (utile se il sito cambia).

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
| `LIST_URL` | pagina da monitorare (default: concorsi di Milano) |
| `CHECK_INTERVAL_MINUTES` | frequenza dei controlli (default 1440 = 24 ore) |
| `MAX_LIST_PAGES`, `MAX_DETAIL_PAGES` | limiti di pagine lette per controllo |
| `DATA_DIR` / `DATABASE_URL` | SQLite su disco oppure Postgres |
| `CRON_TOKEN` | per avviare un controllo da fuori: `/cron?token=...` |

## In locale

```bash
pip install -r requirements.txt
ALERT_EMAIL=tu@esempio.it RESEND_API_KEY=re_... python app.py
```
