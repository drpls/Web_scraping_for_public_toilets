# Documento Tecnico: Tecniche di Scraping e Architettura Software
**Oggetto:** Estrazione dati sui servizi igienici pubblici da Google Maps (multi-città: Venezia, Roma).

## 1. Introduzione
Il software è progettato per automatizzare la ricerca e l'estrazione di metadati, recensioni e foto relativi ai bagni pubblici da Google Maps, con supporto multi-città (attualmente Venezia e Roma). L'analisi di oltre 10 progetti open-source ha dimostrato che l'uso delle API ufficiali (Google Places API) non è sufficiente per ottenere il testo completo delle recensioni, rendendo necessaria l'automazione via browser.

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
* **Riciclo del browser:** Chiusura e riapertura del browser ogni N luoghi per azzerare lo stato del fingerprint.
* **Backoff su CAPTCHA:** Attesa esponenziale con retry automatico in caso di CAPTCHA, fino a un massimo configurabile di tentativi.

### 2.4. Filtraggio Intelligente tramite LLM (Fase 1.5)
Dopo la scoperta dei candidati (Fase 1), molti risultati sono falsi positivi (parcheggi, stazioni, chiese, ecc.). Il software utilizza un **LLM gratuito** tramite OpenRouter per classificare automaticamente ogni candidato come vero bagno pubblico o falso positivo.
* **Modello predefinito:** [NVIDIA Nemotron 3 Super](https://openrouter.ai/nvidia/nemotron-3-super-120b-a12b:free) (120B parametri totali, 12B attivi — architettura Mixture-of-Experts), disponibile gratuitamente su OpenRouter.
* **Classificazione strutturata:** Il modello riceve nome, indirizzo e categoria di ciascun candidato e restituisce un JSON strutturato con la classificazione e la motivazione.
* **Rate limiting:** Rispetta il limite di 20 richieste/minuto del tier gratuito con un limiter interno.
* **Fallback sicuro:** In caso di errore dell'API, l'intero batch viene mantenuto (meglio un falso positivo che perdere un vero bagno).

### 2.5. Estrazione Dati e Manutenzione del Codice
I selettori CSS di Google Maps cambiano frequentemente (classi offuscate).
* **Tecnica:** Centralizzazione di tutti i selettori CSS/XPath in un unico file (`gmaps_selectors.py`). Questo permette di aggiornare rapidamente il software quando Google modifica il DOM, senza dover riscrivere la logica di scraping. Dove possibile, vengono preferiti attributi stabili (`data-*` o ruoli ARIA).
* **Validazione:** Utilizzo di **Pydantic v2** per validare i dati estratti al confine del parsing, assicurando che ogni recensione abbia i campi obbligatori (autore, voto, testo, data).

### 2.6. Strategia di Ricerca Multipla e Deduplicazione
Poiché i bagni pubblici possono avere etichette diverse, il software non si limita a una singola ricerca.
* **Multi-Query:** Il sistema esegue ricerche incrociate utilizzando termini in italiano ("bagni pubblici", "servizi igienici") e in inglese ("public toilet", "restroom"), oltre a ricerche basate su coordinate geografiche.
* **Multi-Città:** Il sistema supporta più città configurabili; attualmente Venezia e Roma, con query di ricerca specifiche per ciascuna.
* **Deduplicazione:** I risultati vengono unificati ed estratti i duplicati utilizzando l'identificativo univoco `place_id` di Google Maps.

### 2.7. Download Foto
Il software scarica le foto di ciascun bagno pubblico da Google Maps.
* **Compressione automatica:** Le foto che superano una dimensione configurabile vengono ri-compresse in JPEG a qualità progressivamente ridotta.
* **Organizzazione:** Le foto sono salvate in sottocartelle per città e luogo.
* **Ripresa:** I luoghi con foto già scaricate vengono saltati automaticamente al riavvio.

## 3. Architettura e Flusso di Esecuzione (Pipeline)
Il flusso di lavoro è diviso in cinque fasi distinte, orchestrate tramite `asyncio`:
1. **Fase 0 (Auth):** Avvio browser con profilo persistente.
2. **Fase 1 (Search):** Navigazione su Maps, esecuzione delle query, scroll dei risultati e salvataggio dei `place_id` unici su database SQLite.
3. **Fase 1.5 (Filtro LLM):** Classificazione dei candidati tramite NVIDIA Nemotron 3 Super su OpenRouter; scarto dei falsi positivi.
4. **Fase 2 (Scraping):** Per ogni luogo, apertura della pagina, click sulla scheda recensioni, ordinamento per "Più recenti", scroll per caricare le recensioni ed estrazione dei dati.
5. **Fase 3 (Foto):** Download e compressione automatica delle foto per ciascun luogo.
6. **Fase 4 (Export):** Interrogazione del DB asincrono (`aiosqlite`) ed esportazione dei dati in formato CSV tramite `pandas`.

La pipeline è progettata per **sopravvivere a esecuzioni lunghe**:
- **Resumabilità:** I luoghi già processati vengono saltati al riavvio.
- **Rate limiting globale:** Token bucket per limitare i luoghi/minuto.
- **Riciclo browser:** Chiusura e rilancio ogni N luoghi per ridurre il fingerprint.
- **Backoff CAPTCHA:** Attesa esponenziale con retry automatico.

## 4. Stack Tecnologico
* **Linguaggio:** Python 3.11+
* **Automazione:** Playwright
* **Database:** SQLite (tramite `aiosqlite` per operazioni asincrone non bloccanti)
* **Validazione Dati:** Pydantic v2
* **Export Dati:** Pandas
* **Client HTTP:** httpx (per le chiamate all'API OpenRouter)
* **LLM (Filtro):** NVIDIA Nemotron 3 Super via OpenRouter (gratuito)
* **Logging & UI:** `structlog` per i log, `rich` per le barre di avanzamento a terminale.

## 5. Approccio "Fail-Fast" (Test di Fattibilità)
Prima di costruire l'intera pipeline, il progetto prevede l'esecuzione obbligatoria di uno script di test (`test_viability.py`). Questo script tenta di estrarre esattamente 6 recensioni da un bagno pubblico noto a Venezia. Se il test fallisce (es. i selettori sono obsoleti o il muro EEA blocca l'accesso), il problema viene identificato in pochi secondi, permettendo di correggere il tiro prima di investire tempo nello sviluppo completo.

---

## 6. Installazione e Utilizzo

### Prerequisiti
* Python 3.11+
* Un account Google (per il login una tantum richiesto dal muro EEA)
* Una chiave API OpenRouter gratuita ([ottienila qui](https://openrouter.ai/keys))

### Installazione Rapida

```bash
cd Web_scraping_public_toilets

# Installa le dipendenze Python
pip install playwright aiosqlite pandas pydantic structlog rich pytest pytest-asyncio httpx python-dotenv

# Installa il browser Chromium per Playwright
python -m playwright install chromium
```

### Configurazione

```bash
# Copia il file di esempio e inserisci la tua chiave API
cp .env.example .env
# Modifica .env con il tuo editor preferito e inserisci OPENROUTER_API_KEY
```

Variabili configurabili in `.env`:
| Variabile | Predefinito | Descrizione |
|-----------|-------------|-------------|
| `OPENROUTER_API_KEY` | *(obbligatorio)* | Chiave API OpenRouter |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Modello LLM per il filtro (mantenere il suffisso `:free`) |
| `OPENROUTER_RPM` | `18` | Limite richieste/minuto (il cap gratuito è 20) |

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
- `--max-reviews N`: Numero massimo di recensioni da estrarre per luogo (predefinito: 5000).

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
├── config.py                   # Configurazione (città, termini di ricerca, ritardi, modello LLM)
├── models.py                   # Schemi Pydantic v2 (PublicRestroom, Review, ExtractionStats)
├── gmaps_selectors.py          # Selettori CSS centralizzati per il DOM di Google Maps
├── pipeline.py                 # Orchestratore completo (ricerca → filtro LLM → recensioni → foto → esportazione)
├── main.py                     # Punto di ingresso CLI (run/test/export/stats)
├── test_viability.py           # Test di fattibilità: 6 recensioni da un bagno pubblico a Venezia
├── .env.example                # Template delle variabili d'ambiente (chiave OpenRouter, modello)
├── README.md                   # Questo file
├── extractor/
│   ├── __init__.py
│   ├── auth.py                 # Profilo Chrome persistente + autenticazione SEE
│   ├── anti_detect.py          # Modalità stealth, ritardi casuali, scorrimento umano, backoff CAPTCHA
│   ├── search_scraper.py       # Fase 1: Ricerca di bagni pubblici su Google Maps
│   ├── openrouter_filter.py    # Fase 1.5: Filtro LLM (Nemotron 3 Super via OpenRouter)
│   ├── review_scraper.py       # Fase 2: Scraping delle recensioni per ogni luogo
│   ├── photo_scraper.py        # Fase 3: Download e compressione foto
│   └── utils.py                # Utility condivise (parsing, conversione)
├── storage/
│   ├── __init__.py
│   ├── sqlite_store.py         # Operazioni CRUD asincrone su SQLite (bagni + recensioni + foto)
│   └── csv_exporter.py         # Esportazione CSV tramite Pandas
└── data/                       # Output: Database SQLite + file CSV + foto
    └── photos/                 # Foto scaricate, organizzate per città/luogo
```