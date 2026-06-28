# Documento Tecnico: Tecniche di Scraping e Architettura Software
**Oggetto:** Estrazione dati sui servizi igienici pubblici a Venezia da Google Maps.

## 1. Introduzione
Il software è progettato per automatizzare la ricerca e l'estazione di metadati e recensioni relativi ai bagni pubblici a Venezia direttamente da Google Maps. L'analisi di oltre 10 progetti open-source ha dimostrato che l'uso delle API ufficiali (Google Places API) non è sufficiente per ottenere il testo completo delle recensioni, rendendo necessaria l'automazione via browser.

## 2. Tecniche Principali Adottate

### 2.1. Automazione del Browser (Playwright vs Selenium)
È stata abbandonata la libreria *Selenium* in favore di **Playwright**. 
* **Motivazione:** Playwright offre un supporto asincrono nativo superiore, meccanismi di *auto-wait* (attesa automatica del caricamento degli elementi), non richiede l'installazione di binari esterni (come il Chromedriver) e gestisce in modo più efficiente le modalità "stealth" (antirilevamento).

### 2.2. Gestione del "Muro di Autenticazione" (EEA Auth Wall)
A causa delle normative europee (SEE - Spazio Economico Europeo, che include l'Italia), Google richiede l'autenticazione per visualizzare le recensioni complete.
* **Tecnica:** Implementazione di un **Profilo Chrome Persistente**. Il software lancia il browser utilizzando un profilo utente salvato localmente. L'utente effettua un login manuale a Google una sola volta; le sessioni e i cookie vengono mantenuti per le esecuzioni successive, bypassando i blocchi.

### 2.3. Mitigazione dell'Anti-Bot e Steganografia
Per evitare che Google identifichi il software come un bot e blocchi l'IP o mostri CAPTCHA, sono state adottate diverse tecniche di *anti-detection*:
* **Delay casuali:** Pause variabili (2-5 secondi tra le azioni, 10-15 secondi tra i luoghi).
* **Scroll umanizzato:** Simulazione degli eventi della rotella del mouse invece dello scroll programmatico.
* **Viewport dinamici:** Variazione casuale delle dimensioni della finestra del browser.
* **Stealth mode:** Override della proprietà `navigator.webdriver` per nascondere l'automazione.
* **Gestione Cookie:** Click automatico sui banner di consenso.

### 2.4. Estrazione Dati e Manutenzione del Codice
I selettori CSS di Google Maps cambiano frequentemente (classi offuscate).
* **Tecnica:** Centralizzazione di tutti i selettori CSS/XPath in un unico file (`gmaps_selectors.py`). Questo permette di aggiornare rapidamente il software quando Google modifica il DOM, senza dover riscrivere la logica di scraping. Dove possibile, vengono preferiti attributi stabili (`data-*` o ruoli ARIA).
* **Validazione:** Utilizzo di **Pydantic** per validare i dati estratti al confine del parsing, assicurando che ogni recensione abbia i campi obbligatori (autore, voto, testo, data).

### 2.5. Strategia di Ricerca Multipla e Deduplicazione
Poiché i bagni pubblici possono avere etichette diverse, il software non si limita a una singola ricerca.
* **Multi-Query:** Il sistema esegue ricerche incrociate utilizzando termini in italiano ("bagni pubblici", "servizi igienici") e in inglese ("public toilet", "restroom"), oltre a ricerche basate su coordinate geografiche (bounding box di Venezia).
* **Deduplicazione:** I risultati vengono unificati ed estratti i duplicati utilizzando l'identificativo univoco `place_id` di Google Maps.

## 3. Architettura e Flusso di Esecuzione (Pipeline)
Il flusso di lavoro è diviso in quattro fasi distinte, orchestrate tramite `asyncio`:
1. **Fase 0 (Auth):** Avvio browser con profilo persistente.
2. **Fase 1 (Search):** Navigazione su Maps, esecuzione delle query, scroll dei risultati e salvataggio dei `place_id` unici su database SQLite.
3. **Fase 2 (Scraping):** Per ogni luogo, apertura della pagina, click sulla scheda recensioni, ordinamento per "Più recenti", scroll per caricare le recensioni ed estrazione dei dati.
4. **Fase 3 (Export):** Interrogazione del DB asincrono (`aiosqlite`) ed esportazione dei dati in formato CSV tramite `pandas`.

## 4. Stack Tecnologico
* **Linguaggio:** Python
* **Automazione:** Playwright
* **Database:** SQLite (tramite `aiosqlite` per operazioni asincrone non bloccanti)
* **Validazione Dati:** Pydantic v2
* **Export Dati:** Pandas
* **Logging & UI:** `structlog` per i log, `rich` per le barre di avanzamento a terminale.

## 5. Approccio "Fail-Fast" (Test di Fattibilità)
Prima di costruire l'intera pipeline, il progetto prevede l'esecuzione obbligatoria di uno script di test (`test_viability.py`). Questo script tenta di estrarre esattamente 6 recensioni da un bagno pubblico noto a Venezia. Se il test fallisce (es. i selettori sono obsoleti o il muro EEA blocca l'accesso), il problema viene identificato in pochi secondi, permettendo di correggere il tiro prima di investire tempo nello sviluppo completo.

---

## 6. Installazione e Utilizzo

### Installazione Rapida

```bash
cd Web_scraping_public_toilets

# Installa le dipendenze Python
pip install playwright aiosqlite pandas pydantic structlog rich pytest pytest-asyncio

# Installa il browser Chromium per Playwright
python -m playwright install chromium
```

### Esecuzione del Test di Fattibilità

Prima di eseguire l'intera pipeline, verifica che tutto funzioni correttamente tramite il test di fattibilità:

```bash
python test_viability.py
```

Questa operazione:
1. Avvierà un browser Chromium in modalità visibile.
2. Navigerà su Google Maps e cercherà "bagno pubblico Venezia".
3. Cliccherà sul primo risultato relativo a un bagno pubblico.
4. Aprirà la scheda delle recensioni ed estrarrà 6 recensioni.
5. Salverà i risultati in `data/test_output.json`.

**Note per la prima esecuzione:**
- Il browser si apre in modalità visibile (non headless) per permetterne l'osservazione.
- Se Google mostra un banner di consenso, accettalo manualmente.
- Se appare il muro di autenticazione SEE, effettua l'accesso a Google una sola volta (la sessione verrà salvata in `~/.aleessiaaaa/chrome-profile/`).

### Esecuzione della Pipeline Completa

```bash
python main.py run
```

Opzioni disponibili:
- `--headless`: Esegue il browser in modalità headless (senza interfaccia grafica).
- `--max-reviews N`: Numero massimo di recensioni da estrarre per luogo (predefinito: 50).

### Esportazione dei Dati Esistenti in CSV

```bash
python main.py export
```

Genera i file `data/csv/restrooms.csv` e `data/csv/reviews.csv`.

### Visualizzazione delle Statistiche del Database

```bash
python main.py stats
```

### Database SQLite

Il database è memorizzato in `data/restrooms.db` e contiene due tabelle:
- **restrooms**: `place_id`, `name`, `city`, `address`, `lat`, `lng`, `rating`, `review_count`, ecc.
- **reviews**: `review_id`, `place_id`, `author`, `rating`, `text`, `date`, `language`.

---

## 7. Struttura del Progetto

```text
Web_scraping_public_toilets/
├── pyproject.toml              # Configurazione di build e dipendenze
├── config.py                   # Configurazione (città, termini di ricerca, ritardi)
├── models.py                   # Schemi Pydantic v2 (PublicRestroom, Review)
├── gmaps_selectors.py          # Selettori CSS centralizzati per il DOM di Google Maps
├── pipeline.py                 # Orchestratore completo (ricerca -> recensioni -> esportazione)
├── main.py                     # Punto di ingresso CLI (run/test/export/stats)
├── test_viability.py           # Test di fattibilità: 6 recensioni da un bagno pubblico a Venezia
├── README.md                   # Questo file
├── extractor/
│   ├── __init__.py
│   ├── auth.py                 # Profilo Chrome persistente + autenticazione SEE
│   ├── anti_detect.py          # Modalità stealth, ritardi casuali, scorrimento umano
│   ├── search_scraper.py       # Fase 1: Ricerca di bagni pubblici su Google Maps
│   └── review_scraper.py       # Fase 2: Scraping delle recensioni per ogni luogo
├── storage/
│   ├── __init__.py
│   ├── sqlite_store.py         # Operazioni CRUD asincrone su SQLite (bagni + recensioni)
│   └── csv_exporter.py         # Esportazione CSV tramite Pandas
└── data/                       # Output: Database SQLite + file CSV
```