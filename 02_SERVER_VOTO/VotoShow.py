"""
VotoShow.py
Server di voto/richiesta canzoni per gli ospiti dello show, in esecuzione
DURANTE lo show sulla rete ospiti. Usa solo la libreria standard di
Python (http.server, json, html, uuid, sqlite3): nessuna dipendenza
esterna, per restare il piu' leggero e stabile possibile mentre lo show
e' live.

Ogni voto e ogni sequenza mandata in coda vengono registrati anche in un
database SQLite (LOG/storico_voti.sqlite3) per tenere uno storico nel
tempo tra una stagione e l'altra - vedi la scheda "Storico" di
ManagerShow.pyw per consultarlo/esportarlo.

Fase 2: se [TRIGGER_LOR] Abilita_Controllo_LOR = True nell'ini, un thread
separato (ponte_lor) controlla la REST API di LOR, aspetta la fine della
canzone in corso, fa partire quella piu' votata e azzera il turno. Se
disabilitato, si torna al comportamento Fase 1: solo voto/classifica,
reset manuale dalla pagina /risultati.

Pagine:
  GET  /              pagina di benvenuto (prima cosa che vedono gli
                       ospiti, es. via redirect automatico del Fritz!Box)
  GET  /vota           pagina di voto per gli ospiti
  GET  /risultati      pagina classifica per l'operatore (non collegata
                       dalla pagina ospiti)
  GET  /copertine/...  immagini di copertina
  GET  /benvenuto/...  immagine/audio della pagina di benvenuto
  POST /vota           registra/aggiorna il voto del dispositivo corrente
  POST /reset           azzera i voti per iniziare un nuovo turno
"""

import configparser
import html
import json
import logging
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from traduzioni import LINGUA_DEFAULT, testo

INTERVALLO_POLLING_LOR_SECONDI = 3
SOGLIA_FINE_CANZONE_SECONDI = 6

# Raffreddamento (vedi _crea_stato_raffreddamento/ponte_lor): una sequenza
# appena andata in onda non puo' rivincere subito. "turni" = per un tot di
# sequenze diverse mandate in onda dopo di lei; "minuti" = per un tot di
# minuti di orologio, indipendentemente da cosa passa nel mezzo.
RAFFREDDAMENTO_TIPO_VALIDO = ("turni", "minuti")
RAFFREDDAMENTO_TIPO_DEFAULT = "turni"
RAFFREDDAMENTO_VALORE_DEFAULT = 3
NOME_COOKIE_LINGUA = "lingua"
LINGUE_VALIDE = ("it", "en")
NOME_COOKIE_ADMIN = "token_admin"
INTERVALLO_MINIMO_RICHIESTA_SECONDI = 2.0  # rate limiting: min secondi tra due POST dello stesso IP

# Strategie di costruzione della coda (vedi ponte_lor per il dettaglio):
# "turno" = vecchio comportamento, ogni turno sostituisce la coda; "persistente"
# = coda che si accumula senza limiti; "tetto" = via di mezzo, coda che si
# accumula fino a un numero massimo di posizioni configurabile.
MODALITA_CODA_VALIDE = ("turno", "persistente", "tetto")
MODALITA_CODA_DEFAULT = "persistente"
TETTO_CODA_DEFAULT = 3

SCRIPT_DIR = Path(__file__).resolve().parent
CARTELLA_AUTOMAZIONE = SCRIPT_DIR.parent / "01_AUTOMAZIONE"
CONFIG_PATH = CARTELLA_AUTOMAZIONE / "CONFIGURAZIONE_SHOW.ini"
FILE_CATALOGO = SCRIPT_DIR / "catalogo_canzoni.json"
CARTELLA_COPERTINE = SCRIPT_DIR / "COPERTINE"
CARTELLA_BENVENUTO = SCRIPT_DIR / "BENVENUTO"
CARTELLA_INFO = SCRIPT_DIR / "INFO"
CARTELLA_LOG = SCRIPT_DIR / "LOG"
FILE_STATO_VOTI = CARTELLA_LOG / "voti_stato.json"
FILE_DATABASE_STORICO = CARTELLA_LOG / "storico_voti.sqlite3"
_lock_database = threading.Lock()

# Configurazione della pagina di benvenuto (letta una volta all'avvio da
# [BENVENUTO] in CONFIGURAZIONE_SHOW.ini, editabile dalla GUI ManagerShow):
# testo libero, nome file immagine/audio dentro CARTELLA_BENVENUTO, colore
# della fascia testo. Campi vuoti = elemento non mostrato.
configurazione_benvenuto: dict = {}

# Configurazione della pagina "Info" (letta una volta all'avvio da [INFO]
# in CONFIGURAZIONE_SHOW.ini, editabile dalla GUI ManagerShow): stessa
# struttura della pagina di benvenuto (testo/immagine/audio/colore), ma
# pensata per contenuto da poter riconsultare quando serve (regole dello
# show, sponsor, beneficenza, ecc.), raggiungibile da un link su /menu e
# /vota-intro - non solo vista una volta all'ingresso come il benvenuto.
configurazione_info: dict = {}

# Modalita' di costruzione della coda attualmente in uso (letta una volta
# all'avvio, vedi ponte_lor): mostrata anche in pagina_risultati() cosi'
# la pagina che vede l'operatore e' sempre coerente con la scelta fatta
# in ManagerShow, invece di dare per scontata una modalita' che magari
# non e' quella attiva in questo momento.
modalita_coda_attiva: str = MODALITA_CODA_DEFAULT
tetto_coda_attivo: int = TETTO_CODA_DEFAULT

# True se la funzione "saluto di Babbo Natale" (nome bambino -> scroll +
# audio personalizzato) e' abilitata da ManagerShow (letta una volta
# all'avvio, vedi avvia_server). E' un programma pensato per essere
# distribuito liberamente: chi non vuole usare questa funzione la lascia
# disattivata e /menu, /saluto, /saluto/nome smettono di esistere (redirect
# a /vota-intro), senza dover toccare codice.
saluto_babbonatale_abilitato: bool = False

NOME_COOKIE_ELETTORE = "id_elettore"

# Stato in memoria del turno di voto corrente: {id_elettore: (id_canzone, timestamp, e' un voto di test)}.
# Il timestamp e' quello dell'ultima volta che l'elettore ha scelto QUELLA
# canzone (si aggiorna anche se cambia idea piu' volte): serve per spareggiare
# i pareggi in calcola_classifica() dando priorita' a chi ha votato per primo.
# Il terzo campo (test) e' True se il voto arriva da loopback (127.0.0.1),
# cioe' dal pulsante "Test multi-voto" della GUI, mai da un ospite vero:
# serve a non far confluire i voti di prova nello storico reale (vedi
# _registra_voto_db/_registra_vincitore_db).
voti_correnti: dict[str, tuple[str, float, bool]] = {}
catalogo_canzoni: list[dict] = []

# Coda salvata dall'ultimo turno con richieste chiare (vedi ponte_lor):
# condivisa col server HTTP solo per mostrarla nella pagina risultati,
# il ponte con LOR e' l'unico che la scrive davvero.
coda_classifica_condivisa: list[dict] = []

# Classifica del turno che ha determinato la sequenza ATTUALMENTE in
# onda (vincitore + resto), catturata nel momento esatto in cui e' stata
# decisa - condivisa solo per mostrarla nella pagina risultati.
classifica_vincente_condivisa: list[dict] = []

# Id delle canzoni attualmente in pausa per raffreddamento (appena andate
# in onda, non possono rivincere per ora): il server HTTP la consulta per
# decidere come trattare un voto su una di queste (vedi do_POST /vota) e
# per etichettarle "in pausa" nella pagina risultati.
id_in_pausa_condiviso: set = set()

# Rate limiting per IP sulle richieste POST (voti/reset/login): ultimo
# istante (time.monotonic) in cui ogni IP ha effettuato una richiesta.
_ultima_richiesta_per_ip: dict[str, float] = {}
_lock_rate_limit = threading.Lock()

# voti_correnti e' letto/scritto da molti thread insieme (un thread per
# richiesta HTTP, piu' il thread ponte_lor): senza questo lock, un voto
# che arriva mentre e' in corso il calcolo della classifica puo' far
# esplodere l'iterazione del dizionario ("dictionary changed size during
# iteration") - un crash inutile con tanti ospiti che votano insieme.
_lock_voti = threading.Lock()

MAX_LUNGHEZZA_CORPO_POST = 4096  # id canzone e password admin sono corti: oltre questo e' anomalo
MAX_CONNESSIONI_CONTEMPORANEE = 40  # oltre questo tetto il PC dello show va protetto, non il voto
TIMEOUT_SOCKET_SECONDI = 10  # chiude connessioni aperte che non mandano dati (slowloris)


def _sanifica_testo_pubblico(valore: str) -> str:
    """Rimuove caratteri che non devono comparire in un dato inviato dal
    pubblico (id canzone, ecc.), per non lasciar passare frammenti di
    markup/HTML verso il resto del programma."""
    for carattere in ("<", ">", "/"):
        valore = valore.replace(carattere, "")
    return valore


# ----------------------------------------------------------------------
# Avvio: logging e caricamento dati
# ----------------------------------------------------------------------
def configura_logging() -> logging.Logger:
    CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
    percorso_log = CARTELLA_LOG / f"VotoShow_{datetime.now():%Y%m%d}.log"

    logger = logging.getLogger("VotoShow")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formato = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler_file = logging.FileHandler(percorso_log, encoding="utf-8")
    handler_file.setFormatter(formato)
    logger.addHandler(handler_file)

    handler_console = logging.StreamHandler(sys.stdout)
    handler_console.setFormatter(formato)
    logger.addHandler(handler_console)

    return logger


def leggi_porta_server(logger: logging.Logger) -> int:
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti:
        logger.warning("CONFIGURAZIONE_SHOW.ini non trovato, uso porta di default 8080")
        return 8080
    return parser.getint("VOTO", "Porta_Server", fallback=8080)


def leggi_token_admin(logger: logging.Logger) -> str:
    """Password per /risultati e /reset. Vuota = nessuna protezione."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti:
        return ""
    return parser.get("VOTO", "Token_Amministratore", fallback="").strip()


def leggi_modalita_coda(logger: logging.Logger) -> tuple:
    """Legge da [VOTO] come costruire la coda delle prossime sequenze
    (vedi ponte_lor): Modalita_Coda ("turno"/"persistente"/"tetto") e, se
    "tetto", Tetto_Coda (numero massimo di posizioni in coda)."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti:
        return MODALITA_CODA_DEFAULT, TETTO_CODA_DEFAULT

    modalita = parser.get("VOTO", "Modalita_Coda", fallback=MODALITA_CODA_DEFAULT).strip().lower()
    if modalita not in MODALITA_CODA_VALIDE:
        logger.warning(
            "[Coda] Modalita_Coda '%s' non valida (valori ammessi: %s), uso '%s'",
            modalita, ", ".join(MODALITA_CODA_VALIDE), MODALITA_CODA_DEFAULT,
        )
        modalita = MODALITA_CODA_DEFAULT

    tetto = parser.getint("VOTO", "Tetto_Coda", fallback=TETTO_CODA_DEFAULT)
    if tetto < 1:
        tetto = TETTO_CODA_DEFAULT

    return modalita, tetto


def leggi_raffreddamento(logger: logging.Logger) -> tuple:
    """Legge da [VOTO] il raffreddamento delle sequenze appena andate in
    onda: Raffreddamento_Tipo ("turni"/"minuti") e Raffreddamento_Valore
    (quanti turni, o quanti minuti)."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti:
        return RAFFREDDAMENTO_TIPO_DEFAULT, RAFFREDDAMENTO_VALORE_DEFAULT

    tipo = parser.get("VOTO", "Raffreddamento_Tipo", fallback=RAFFREDDAMENTO_TIPO_DEFAULT).strip().lower()
    if tipo not in RAFFREDDAMENTO_TIPO_VALIDO:
        logger.warning(
            "[Raffreddamento] Raffreddamento_Tipo '%s' non valido (valori ammessi: %s), uso '%s'",
            tipo, ", ".join(RAFFREDDAMENTO_TIPO_VALIDO), RAFFREDDAMENTO_TIPO_DEFAULT,
        )
        tipo = RAFFREDDAMENTO_TIPO_DEFAULT

    valore = parser.getint("VOTO", "Raffreddamento_Valore", fallback=RAFFREDDAMENTO_VALORE_DEFAULT)
    if valore < 1:
        valore = RAFFREDDAMENTO_VALORE_DEFAULT

    return tipo, valore


def _crea_stato_raffreddamento(tipo: str, valore: int) -> dict:
    if tipo == "minuti":
        return {"tipo": "minuti", "valore": valore, "ultima_riproduzione": {}}
    return {"tipo": "turni", "valore": valore, "cronologia": deque(maxlen=valore)}


def _in_raffreddamento(stato: dict, id_canzone: str) -> bool:
    if stato["tipo"] == "minuti":
        ultima = stato["ultima_riproduzione"].get(id_canzone)
        return ultima is not None and (time.time() - ultima) < stato["valore"] * 60
    return id_canzone in stato["cronologia"]


def _registra_riproduzione(stato: dict, id_canzone: str) -> None:
    if stato["tipo"] == "minuti":
        stato["ultima_riproduzione"][id_canzone] = time.time()
    else:
        stato["cronologia"].append(id_canzone)


def leggi_configurazione_benvenuto(logger: logging.Logger) -> dict:
    """Legge [BENVENUTO] (testo/immagine/audio/colore della pagina che gli
    ospiti vedono per prima, prima di arrivare al voto). Tutti i campi sono
    opzionali: una pagina di benvenuto minimale funziona anche vuota."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti or "BENVENUTO" not in parser:
        return {"testo": "", "immagine": "", "audio": "", "colore": "#1e1e1e"}

    sezione = parser["BENVENUTO"]
    # ManagerShow.pyw salva gli a-capo del testo come '\n' letterale (un
    # valore ini sta su una sola riga): qui li rimettiamo a posto.
    testo_grezzo = sezione.get("Testo", fallback="")
    return {
        "testo": testo_grezzo.replace("\\n", "\n").strip(),
        "immagine": sezione.get("File_Immagine", fallback="").strip(),
        "audio": sezione.get("File_Audio", fallback="").strip(),
        "colore": sezione.get("Colore_Sfondo", fallback="#1e1e1e").strip() or "#1e1e1e",
    }


def leggi_configurazione_info(logger: logging.Logger) -> dict:
    """Legge [INFO] (pagina Beneficenza facoltativa raggiungibile da /menu
    e /vota-intro, es. regole/sponsor/beneficenza). Tutti i campi di
    contenuto sono opzionali - la visibilita' e' decisa SOLO da "Visibile"
    (vedi _info_configurata), non piu' dal fatto che siano vuoti o no."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti or "INFO" not in parser:
        return {
            "testo": "", "immagine": "", "audio": "", "colore": "#1e1e1e",
            "visibile": False, "link_donazione": "", "testo_grazie": "",
        }

    sezione = parser["INFO"]
    testo_grezzo = sezione.get("Testo", fallback="")
    testo_grazie_grezzo = sezione.get("Testo_Grazie", fallback="")
    return {
        "testo": testo_grezzo.replace("\\n", "\n").strip(),
        "immagine": sezione.get("File_Immagine", fallback="").strip(),
        "audio": sezione.get("File_Audio", fallback="").strip(),
        "colore": sezione.get("Colore_Sfondo", fallback="#1e1e1e").strip() or "#1e1e1e",
        "visibile": sezione.getboolean("Visibile", fallback=False),
        "link_donazione": sezione.get("Link_Donazione", fallback="").strip(),
        "testo_grazie": testo_grazie_grezzo.replace("\\n", "\n").strip(),
    }


def leggi_saluto_babbonatale(logger: logging.Logger) -> bool:
    """Legge [SALUTO_BABBONATALE] Abilita (True/False, default False):
    interruttore generale della funzione "saluto personalizzato per nome",
    pensato per un pubblico che distribuisce questo programma liberamente
    e potrebbe non volerne fare uso. Non serve nient'altro qui: i dettagli
    veri (voce, dizionario nomi, ecc.) vivono nel modulo di generazione
    separato, non in VotoShow.py."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti or "SALUTO_BABBONATALE" not in parser:
        return False
    return parser.getboolean("SALUTO_BABBONATALE", "Abilita", fallback=False)


def leggi_configurazione_lor(logger: logging.Logger):
    """Legge [TRIGGER_LOR] (se/come collegarsi a LOR) e [SEQUENZE_LOR]
    (nome esatto di ogni canzone come compare in LOR, usato per
    mainSequences/playNext). Ritorna (abilitato, porta_api, mappa_nomi_lor)."""
    parser = configparser.ConfigParser()
    letti = parser.read(CONFIG_PATH, encoding="utf-8")
    if not letti or "TRIGGER_LOR" not in parser:
        logger.info("[LOR] Sezione [TRIGGER_LOR] non trovata: integrazione con LOR disattivata")
        return False, 8001, {}

    sezione = parser["TRIGGER_LOR"]
    abilitato = sezione.getboolean("Abilita_Controllo_LOR", fallback=False)
    porta_api = sezione.getint("Porta_API_LOR", fallback=8001)

    mappa_nomi_lor = {}
    if "SEQUENZE_LOR" in parser:
        mappa_nomi_lor = {
            chiave: valore.strip()
            for chiave, valore in parser["SEQUENZE_LOR"].items()
            if valore.strip()
        }
    return abilitato, porta_api, mappa_nomi_lor


def carica_catalogo(logger: logging.Logger) -> list[dict]:
    if not FILE_CATALOGO.is_file():
        logger.error(
            "Catalogo canzoni non trovato (%s). Esegui prima CostruisciCatalogo.py. "
            "Il server parte comunque, ma la pagina di voto sara' vuota.",
            FILE_CATALOGO,
        )
        return []
    try:
        with open(FILE_CATALOGO, "r", encoding="utf-8") as f:
            catalogo = json.load(f)
        logger.info("Catalogo caricato: %d canzoni votabili", len(catalogo))
        return catalogo
    except (OSError, json.JSONDecodeError) as errore:
        logger.error("Impossibile leggere il catalogo canzoni: %s", errore)
        return []


def carica_stato_voti_precedente(logger: logging.Logger) -> None:
    """Ripristina i voti dell'ultimo turno se il server e' stato riavviato
    per errore a meta' show, cosi' non si perde nulla per un crash."""
    if not FILE_STATO_VOTI.is_file():
        return
    try:
        with open(FILE_STATO_VOTI, "r", encoding="utf-8") as f:
            dati = json.load(f)
        with _lock_voti:
            for id_elettore, valore in dati.items():
                if isinstance(valore, list) and len(valore) == 3:
                    voti_correnti[id_elettore] = (valore[0], valore[1], bool(valore[2]))
        logger.info("Ripristinati %d voti dal turno precedente", len(voti_correnti))
    except (OSError, json.JSONDecodeError) as errore:
        logger.warning("Impossibile ripristinare lo stato voti precedente: %s", errore)


def salva_stato_voti(logger: logging.Logger) -> None:
    try:
        with _lock_voti:
            istantanea = {chiave: list(valore) for chiave, valore in voti_correnti.items()}
        with open(FILE_STATO_VOTI, "w", encoding="utf-8") as f:
            json.dump(istantanea, f, ensure_ascii=False)
    except OSError as errore:
        logger.warning("Impossibile salvare lo stato dei voti su disco: %s", errore)


# ----------------------------------------------------------------------
# Database storico (SQLite, libreria standard): tiene traccia nel tempo
# di ogni voto e di ogni sequenza mandata in coda dal ponte con LOR, cosi'
# da avere uno storico su piu' stagioni (che sequenze piacciono di piu',
# quanti voti al giorno, a che ora, ecc.) - vedi la scheda "Storico" di
# ManagerShow.pyw per consultarlo/esportarlo. I voti generati dal test
# multi-voto (loopback, mai un ospite vero) sono marcati "test" e restano
# fuori dai riepiloghi per default.
# ----------------------------------------------------------------------
def inizializza_database_storico(logger: logging.Logger) -> None:
    try:
        CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
        with _lock_database:
            connessione = sqlite3.connect(FILE_DATABASE_STORICO, timeout=5)
            try:
                connessione.execute("""
                    CREATE TABLE IF NOT EXISTS voti (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        data TEXT NOT NULL,
                        ora TEXT NOT NULL,
                        canzone_id TEXT NOT NULL,
                        canzone_titolo TEXT NOT NULL,
                        elettore_id TEXT NOT NULL,
                        test INTEGER NOT NULL DEFAULT 0
                    )
                """)
                connessione.execute("""
                    CREATE TABLE IF NOT EXISTS vincitori (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        data TEXT NOT NULL,
                        ora TEXT NOT NULL,
                        canzone_id TEXT NOT NULL,
                        canzone_titolo TEXT NOT NULL,
                        voti_ricevuti INTEGER NOT NULL,
                        test INTEGER NOT NULL DEFAULT 0
                    )
                """)
                connessione.execute("CREATE INDEX IF NOT EXISTS idx_voti_data ON voti(data)")
                connessione.execute("CREATE INDEX IF NOT EXISTS idx_vincitori_data ON vincitori(data)")
                connessione.commit()
            finally:
                connessione.close()
    except sqlite3.Error as errore:
        logger.warning("[DB] Impossibile inizializzare lo storico voti: %s", errore)


def _registra_voto_db(logger: logging.Logger, canzone_id: str, canzone_titolo: str, elettore_id: str, test: bool) -> None:
    adesso = datetime.now()
    try:
        with _lock_database:
            connessione = sqlite3.connect(FILE_DATABASE_STORICO, timeout=5)
            try:
                connessione.execute(
                    "INSERT INTO voti (data, ora, canzone_id, canzone_titolo, elettore_id, test) VALUES (?, ?, ?, ?, ?, ?)",
                    (adesso.strftime("%Y-%m-%d"), adesso.strftime("%H:%M:%S"), canzone_id, canzone_titolo, elettore_id, int(test)),
                )
                connessione.commit()
            finally:
                connessione.close()
    except sqlite3.Error as errore:
        logger.warning("[DB] Impossibile registrare il voto nello storico: %s", errore)


def _registra_vincitore_db(logger: logging.Logger, canzone_id: str, canzone_titolo: str, voti_ricevuti: int, test: bool) -> None:
    adesso = datetime.now()
    try:
        with _lock_database:
            connessione = sqlite3.connect(FILE_DATABASE_STORICO, timeout=5)
            try:
                connessione.execute(
                    "INSERT INTO vincitori (data, ora, canzone_id, canzone_titolo, voti_ricevuti, test) VALUES (?, ?, ?, ?, ?, ?)",
                    (adesso.strftime("%Y-%m-%d"), adesso.strftime("%H:%M:%S"), canzone_id, canzone_titolo, voti_ricevuti, int(test)),
                )
                connessione.commit()
            finally:
                connessione.close()
    except sqlite3.Error as errore:
        logger.warning("[DB] Impossibile registrare il vincitore nello storico: %s", errore)


# ----------------------------------------------------------------------
# Logica di voto
# ----------------------------------------------------------------------
def calcola_classifica() -> list[dict]:
    """Ritorna il catalogo con conteggio voti e percentuale, ordinato dal
    piu' votato al meno votato. In caso di pareggio nel numero di voti vince
    chi ha il voto attivo piu' vecchio (il primo che ha "consolidato" quella
    scelta in questo turno), cosi' l'ordine e' sempre deterministico e non
    serve piu' lasciare un pareggio irrisolto.

    Ogni voce ha anche "test": True solo se TUTTI i voti attivi per quella
    canzone in questo turno arrivano da loopback (test multi-voto) - basta
    un solo voto vero a far scattare False, cosi' un turno con dentro
    anche un solo ospite reale conta come reale nello storico (vedi
    _registra_vincitore_db in ponte_lor)."""
    conteggi = {}
    timestamp_piu_vecchio = {}
    tutti_test = {}
    with _lock_voti:
        istantanea_voti = list(voti_correnti.values())
    for id_canzone, timestamp, test in istantanea_voti:
        conteggi[id_canzone] = conteggi.get(id_canzone, 0) + 1
        if id_canzone not in timestamp_piu_vecchio or timestamp < timestamp_piu_vecchio[id_canzone]:
            timestamp_piu_vecchio[id_canzone] = timestamp
        tutti_test[id_canzone] = tutti_test.get(id_canzone, True) and test

    totale_voti = sum(conteggi.values())
    classifica = []
    for canzone in catalogo_canzoni:
        voti = conteggi.get(canzone["id"], 0)
        percentuale = round(100 * voti / totale_voti) if totale_voti else 0
        classifica.append({
            **canzone, "voti": voti, "percentuale": percentuale,
            "test": tutti_test.get(canzone["id"], False) if voti else False,
        })

    classifica.sort(
        key=lambda voce: (-voce["voti"], timestamp_piu_vecchio.get(voce["id"], float("inf")))
    )
    return classifica


def _determina_vincitore(classifica: list[dict]):
    """Ritorna la canzone vincente del turno, oppure None se non ci sono
    voti: in quel caso nessuno vince e la scaletta normale di LOR prosegue
    senza interferenze. I pareggi nel numero di voti sono gia' risolti da
    calcola_classifica() (spareggio per timestamp piu' vecchio), quindi qui
    basta guardare la prima voce."""
    if not classifica or classifica[0]["voti"] == 0:
        return None
    return classifica[0]


# ----------------------------------------------------------------------
# Ponte con LOR (Fase 2): aspetta la fine della canzone in corso, fa
# partire su LOR quella piu' votata, poi azzera i voti per il turno dopo.
# Gira in un thread separato, non blocca mai il server di voto: ogni
# errore di comunicazione con LOR viene solo loggato.
# ----------------------------------------------------------------------
def _chiama_api_lor(metodo: str, percorso: str, porta_api: int):
    url = f"http://127.0.0.1:{porta_api}{percorso}"
    richiesta = urllib.request.Request(url, method=metodo)
    with urllib.request.urlopen(richiesta, timeout=5) as risposta:
        corpo = risposta.read()
    return json.loads(corpo) if corpo else None


def _timespan_a_secondi(valore_timespan: str) -> float:
    try:
        ore, minuti, secondi = valore_timespan.split(":")
        return int(ore) * 3600 + int(minuti) * 60 + float(secondi)
    except (ValueError, AttributeError):
        return 0.0


def _trova_item_id_main_sequence(porta_api: int, nome_sequenza_lor: str) -> str:
    """Cerca in GET /v1/player/mainSequences la voce con questo nome
    esatto (case-insensitive) e ne ritorna l'itemId, da usare con
    playNext. None se non la trova (nome sbagliato in configurazione o
    sequenza non presente nel Main)."""
    risposta = _chiama_api_lor("GET", "/v1/player/mainSequences", porta_api)
    voci = risposta if isinstance(risposta, list) else (risposta or {}).get("value", [])
    for voce in voci:
        if voce.get("name", "").strip().lower() == nome_sequenza_lor.strip().lower():
            return voce.get("itemId")
    return None


def _metti_in_coda_lor(
    logger: logging.Logger, porta_api: int, mappa_nomi_lor: dict, canzone: dict, messaggio_log: str
) -> bool:
    """Cerca la sequenza di 'canzone' in LOR e la manda in coda con
    playNext. Ritorna True se e' andata a buon fine (usato per capire se
    provare la voce successiva della classifica in caso di errore)."""
    nome_lor = mappa_nomi_lor.get(canzone["id"])
    if not nome_lor:
        logger.warning(
            "[LOR] '%s' e' in classifica ma non ha un nome LOR configurato in [SEQUENZE_LOR]",
            canzone["titolo"],
        )
        return False

    item_id = _trova_item_id_main_sequence(porta_api, nome_lor)
    if not item_id:
        logger.warning(
            "[LOR] Sequenza '%s' non trovata in mainSequences: controlla [SEQUENZE_LOR] nell'ini",
            nome_lor,
        )
        return False

    _chiama_api_lor("PUT", f"/v1/player/mainSequences/playNext/{item_id}", porta_api)
    logger.info("[LOR] " + messaggio_log, canzone["titolo"], canzone["voti"])
    return True


def ponte_lor(
    logger: logging.Logger, porta_api: int, mappa_nomi_lor: dict,
    modalita_coda: str = MODALITA_CODA_DEFAULT, tetto_coda: int = TETTO_CODA_DEFAULT,
    tipo_raffreddamento: str = RAFFREDDAMENTO_TIPO_DEFAULT, valore_raffreddamento: int = RAFFREDDAMENTO_VALORE_DEFAULT,
):
    """Quando la canzone in corso sta per finire, manda in CODA (non
    interrompe nulla) la prossima sequenza da suonare usando
    PUT /v1/player/mainSequences/playNext/{itemId}: la canzone attuale
    finisce naturalmente, poi parte quella scelta. mappa_nomi_lor associa
    l'id di ogni canzone del catalogo al nome esatto con cui compare in
    LOR (sezione [SEQUENZE_LOR] dell'ini).

    modalita_coda decide come il risultato di ogni turno si combina con
    quello che c'e' gia' in coda ("coda_persistente"):
      - "turno": il turno SOSTITUISCE sempre la coda (vecchio comportamento,
        nessuna memoria tra un turno e l'altro: vince solo chi vota nel
        turno attuale, chi perde non vede mai partire la propria scelta se
        il pubblico continua a votare).
      - "persistente": il turno si ACCODA in fondo, senza limiti (chi vince
        un turno precedente non perde mai il posto per voti piu' recenti,
        ma in serate molto votate l'attesa puo' allungarsi parecchio).
      - "tetto": come "persistente" ma fino a un massimo di tetto_coda
        posizioni in coda: oltre quel limite i turni in eccesso non si
        accodano (via di mezzo tra i due sopra: attesa massima limitata,
        senza pero' scartare chi e' gia' in coda).
    In tutti i casi, ad ogni fine turno si manda comunque avanti la coda:
    si cerca la prima voce non in pausa da cima a fondo (senza scartare
    quelle in pausa, restano in coda per un turno successivo) e la si
    manda in playNext.

    Le sequenze gia' presenti in coda non vengono duplicate se rivotate
    (restano dov'erano, il loro turno arrivera' comunque). Un pareggio nel
    numero di voti non blocca la decisione: vince chi, tra i pari voti, ha
    il voto attivo piu' vecchio (vedi calcola_classifica). Quando la coda
    si esaurisce (o non c'e' mai stata), la scaletta normale di LOR
    prosegue senza interferenze."""
    global coda_classifica_condivisa, classifica_vincente_condivisa, id_in_pausa_condiviso
    logger.info(
        "[LOR] Ponte con LOR avviato (porta API %d, modalita' coda '%s'%s, raffreddamento %d %s)",
        porta_api, modalita_coda, f", tetto {tetto_coda}" if modalita_coda == "tetto" else "",
        valore_raffreddamento, tipo_raffreddamento,
    )
    id_canzone_lor_precedente = None
    turno_deciso_per = None  # id per cui abbiamo gia' deciso la prossima voce in coda
    coda_persistente = []  # coda che si accumula nel tempo, mai scartata dai voti nuovi
    classifica_vincente = []  # ultima voce mandata in playNext + resto della coda, per la pagina risultati
    stato_raffreddamento = _crea_stato_raffreddamento(tipo_raffreddamento, valore_raffreddamento)

    while True:
        try:
            stato = _chiama_api_lor("GET", "/v1/player/status", porta_api)
            coda_riproduzione = (stato or {}).get("playingQueue", {})
            items = coda_riproduzione.get("items", []) if isinstance(coda_riproduzione, dict) else []

            if not items:
                id_canzone_lor_precedente = None
                turno_deciso_per = None
                coda_persistente = []
                classifica_vincente = []
            else:
                item_corrente = items[0]
                id_canzone_lor_corrente = item_corrente.get("id")

                if id_canzone_lor_corrente != id_canzone_lor_precedente:
                    with _lock_voti:
                        voti_correnti.clear()
                    salva_stato_voti(logger)
                    logger.info(
                        "[LOR] Nuova canzone in riproduzione ('%s'): richieste azzerate per il nuovo turno",
                        item_corrente.get("name"),
                    )
                    id_canzone_lor_precedente = id_canzone_lor_corrente
                    turno_deciso_per = None

                secondi_rimanenti = _timespan_a_secondi(item_corrente.get("remainingDuration", ""))

                if secondi_rimanenti <= SOGLIA_FINE_CANZONE_SECONDI and turno_deciso_per != id_canzone_lor_corrente:
                    classifica = calcola_classifica()
                    # Le sequenze in raffreddamento non possono rivincere
                    # subito: da' un po' di varieta' anche con tanti
                    # utenti che votano sempre la stessa cosa. Restano
                    # comunque votabili e conteggiate, solo non possono
                    # vincere finche' non e' passato il raffreddamento.
                    candidati_idonei = [v for v in classifica if not _in_raffreddamento(stato_raffreddamento, v["id"])]
                    vincitore_turno = _determina_vincitore(candidati_idonei)
                    turno_deciso_per = id_canzone_lor_corrente

                    if vincitore_turno is not None:
                        resto_turno = [
                            voce for voce in classifica
                            if voce["voti"] > 0 and voce["id"] != vincitore_turno["id"]
                        ]
                        turno_ordinato = [vincitore_turno] + resto_turno

                        # Registrato SEMPRE che c'e' un vincitore di turno,
                        # a prescindere da modalita' e da cosa succede poi
                        # alla coda (accodato, gia' presente, scartato per
                        # tetto pieno): ha comunque vinto quel turno con
                        # richieste vere, vale per lo storico/popolarita'
                        # indipendentemente da come la coda lo gestisce.
                        _registra_vincitore_db(
                            logger, vincitore_turno["id"], vincitore_turno["titolo"],
                            vincitore_turno["voti"], vincitore_turno.get("test", False),
                        )

                        if modalita_coda == "turno":
                            # Nessuna coda per davvero: solo il vincitore
                            # del turno, il resto della sua classifica
                            # (2°, 3°...) non viene portato avanti in
                            # nessuna forma - altrimenti "nessuna coda"
                            # sarebbe falso, con la colonna "in coda" che
                            # mostra qualcosa che coda non e'.
                            coda_persistente = [vincitore_turno]
                            logger.info(
                                "[LOR] Nuovo vincitore di turno (modalita' 'turno', nessuna coda): "
                                "'%s' con %d richieste",
                                vincitore_turno["titolo"], vincitore_turno["voti"],
                            )
                        else:
                            # "persistente"/"tetto": si accoda IN FONDO alla
                            # coda esistente (vincitore del turno, poi il
                            # resto della sua classifica) - non la
                            # sostituisce. Chi e' gia' in coda da un turno
                            # precedente non viene duplicato: ha gia' il suo
                            # posto.
                            id_gia_in_coda = {voce["id"] for voce in coda_persistente}
                            nuove_voci = [voce for voce in turno_ordinato if voce["id"] not in id_gia_in_coda]

                            if modalita_coda == "tetto" and nuove_voci:
                                posti_liberi = max(0, tetto_coda - len(coda_persistente))
                                if posti_liberi < len(nuove_voci):
                                    if posti_liberi == 0:
                                        logger.info(
                                            "[LOR] Coda piena (tetto %d): richieste di questo turno per '%s' non accodate",
                                            tetto_coda, vincitore_turno["titolo"],
                                        )
                                    nuove_voci = nuove_voci[:posti_liberi]

                            if nuove_voci:
                                coda_persistente += nuove_voci
                                logger.info(
                                    "[LOR] Richieste di questo turno accodate in fondo (vincitore del turno: '%s', %d richieste)",
                                    vincitore_turno["titolo"], vincitore_turno["voti"],
                                )
                    elif classifica and classifica[0]["voti"] > 0 and not candidati_idonei:
                        logger.info(
                            "[LOR] Tutte le richieste di questo turno sono per sequenze in pausa "
                            "(vinte da poco): restano in coda solo quelle gia' presenti"
                        )

                    # In ogni caso si manda avanti la coda: si cerca da cima a
                    # fondo la prima voce non in pausa e la si manda in
                    # playNext. Le voci in pausa saltate NON vengono scartate
                    # (restano al loro posto per un turno successivo); una
                    # voce che fallisce per errore di configurazione (nome
                    # LOR mancante/sbagliato) viene invece scartata, non ha
                    # senso tenerla in coda se non si puo' mai mandare in
                    # onda, e si prova subito la prossima.
                    prossimo = None
                    indice = 0
                    while indice < len(coda_persistente) and prossimo is None:
                        candidato = coda_persistente[indice]
                        if _in_raffreddamento(stato_raffreddamento, candidato["id"]):
                            indice += 1
                            continue
                        if _metti_in_coda_lor(
                            logger, porta_api, mappa_nomi_lor, candidato,
                            "'%s' messa in coda, con %d richieste",
                        ):
                            prossimo = coda_persistente.pop(indice)
                        else:
                            coda_persistente.pop(indice)  # scartata: la voce dopo scivola su questo indice

                    if prossimo is not None:
                        classifica_vincente = [prossimo] + coda_persistente
                        _registra_riproduzione(stato_raffreddamento, prossimo["id"])
                    else:
                        # Niente da mandare in playNext (coda vuota, o tutto
                        # cio' che c'era in coda e' in pausa): la scaletta
                        # normale di LOR prosegue da qui. La colonna "in
                        # onda ora" non ha piu' un significato affidabile -
                        # senza questo azzeramento resterebbe a mostrare
                        # l'ultima voce decisa anche molte canzoni dopo.
                        classifica_vincente = []
                        if not coda_persistente:
                            logger.info("[LOR] Coda vuota: la scaletta normale di LOR prosegue")

        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as errore:
            logger.warning("[LOR] Impossibile contattare l'API di LOR: %s", errore)
            # Mentre LOR non e' raggiungibile (es. lo si sta chiudendo o
            # riavviando) non sappiamo piu' cosa sia davvero in onda: senza
            # questo azzeramento la colonna "in onda ora" resterebbe
            # bloccata sull'ultimo valore anche a riavvio di LOR avvenuto.
            # La coda vera e propria (coda_persistente) invece NON si
            # tocca: un'interruzione di rete non deve far perdere i voti
            # gia' accumulati in coda.
            classifica_vincente = []
        except Exception as errore:
            logger.error("[LOR] Errore inatteso nel ponte con LOR: %s", errore)

        coda_classifica_condivisa = list(coda_persistente)
        classifica_vincente_condivisa = list(classifica_vincente)
        id_in_pausa_condiviso = {
            canzone["id"] for canzone in catalogo_canzoni
            if _in_raffreddamento(stato_raffreddamento, canzone["id"])
        }
        time.sleep(INTERVALLO_POLLING_LOR_SECONDI)


# ----------------------------------------------------------------------
# Generazione pagine HTML (nessuna dipendenza esterna, solo f-string)
# ----------------------------------------------------------------------
STILE_BASE = """
<style>
  body { font-family: Arial, Helvetica, sans-serif; background:#111; color:#eee;
         margin:0; padding:16px; }
  h1 { font-size:1.4em; text-align:center; }
  .canzone { display:flex; align-items:center; gap:12px; background:#1e1e1e;
             border-radius:10px; padding:10px; margin-bottom:10px; }
  .canzone img { width:64px; height:64px; object-fit:cover; border-radius:6px;
                 background:#333; flex-shrink:0; }
  .canzone .info { flex:1; min-width:0; }
  .canzone .titolo { font-weight:bold; }
  .canzone .artista { color:#aaa; font-size:0.9em; }
  .canzone .durata { color:#888; font-size:0.85em; }
  form.voto button, .reset button { background:#2e7d32; color:#fff; border:none;
             border-radius:6px; padding:10px 14px; font-size:1em; }
  .voce-votata { border:2px solid #2e7d32; }
  .barra { background:#333; border-radius:4px; height:10px; margin-top:6px; overflow:hidden; }
  .barra-interna { background:#2e7d32; height:100%; }
  .percentuale { font-size:0.85em; color:#ccc; }
  .selettore-lingua { text-align:center; margin-bottom:10px; }
  .selettore-lingua a { color:#ccc; text-decoration:none; padding:4px 8px; }
  .selettore-lingua a.attiva { color:#fff; font-weight:bold; text-decoration:underline; }
  .tre-colonne { display:flex; gap:16px; align-items:flex-start; }
  .colonna { flex:1; min-width:0; }
  .colonna h2 { font-size:1.05em; color:#ccc; margin:0 0 8px; }
  .nota-colonna { font-size:0.8em; color:#888; margin:-4px 0 10px; }
  .avviso-pausa { text-align:center; border-radius:8px; padding:10px 14px; margin:0 0 12px; font-size:0.9em; }
  .avviso-rifiutato { background:#4a2020; color:#f5b5b5; }
  .avviso-accettato { background:#203a4a; color:#a9d6f5; }
  .avviso-coda-piena { background:#4a3d20; color:#f5dfa9; }
  .toast-notifica { position:fixed; top:14px; left:50%; width:420px; max-width:92%;
             box-shadow:0 4px 16px rgba(0,0,0,0.45); z-index:1000; cursor:pointer;
             transform:translateX(-50%); transition:opacity 0.4s ease, transform 0.4s ease;
             animation:toast-ingresso 0.3s ease-out; }
  .toast-notifica.toast-nascosta { opacity:0; transform:translateX(-50%) translateY(-16px); }
  @keyframes toast-ingresso { from { opacity:0; transform:translateX(-50%) translateY(-16px); } to { opacity:1; transform:translateX(-50%) translateY(0); } }
  .etichetta-in-pausa { color:#999; font-weight:normal; font-size:0.85em; }
  @media (max-width: 900px) {
    .tre-colonne { flex-direction:column; }
  }
</style>
"""


def _colore_testo_leggibile(colore_sfondo_hex: str) -> str:
    """Nero o bianco, quello che si legge meglio sopra colore_sfondo_hex
    (formula di luminanza percepita standard). Usato per il testo della
    pagina di benvenuto: qualunque colore venga proposto/scelto per la
    fascia (anche preso a caso da una foto), il testo sopra resta sempre
    leggibile senza dover scegliere anche quello a mano."""
    grezzo = colore_sfondo_hex.lstrip("#")
    if len(grezzo) != 6:
        return "#eee"
    try:
        r, g, b = int(grezzo[0:2], 16), int(grezzo[2:4], 16), int(grezzo[4:6], 16)
    except ValueError:
        return "#eee"
    luminanza = 0.299 * r + 0.587 * g + 0.114 * b
    return "#111" if luminanza > 150 else "#eee"


def _selettore_lingua_html(lingua: str, percorso_pagina: str) -> str:
    def classe(codice):
        return "attiva" if codice == lingua else ""
    return f"""
    <div class="selettore-lingua">
      <a class="{classe('it')}" href="/lingua?imposta=it&torna={percorso_pagina}">IT</a> |
      <a class="{classe('en')}" href="/lingua?imposta=en&torna={percorso_pagina}">EN</a>
    </div>
    """


def pagina_voto(id_elettore: str, lingua: str = LINGUA_DEFAULT, avviso: str = "") -> str:
    with _lock_voti:
        voto = voti_correnti.get(id_elettore)
    voto_attuale = voto[0] if voto else None
    righe = []
    if not catalogo_canzoni:
        righe.append(f"<p>{testo(lingua, 'nessuna_canzone')}</p>")
    for canzone in catalogo_canzoni:
        selezionata = canzone["id"] == voto_attuale
        classe_extra = " voce-votata" if selezionata else ""
        immagine_html = (
            f'<img src="/copertine/{html.escape(canzone["copertina"])}" alt="">'
            if canzone.get("copertina")
            else '<img src="" alt="" style="visibility:hidden">'
        )
        etichetta_bottone = testo(lingua, "bottone_votato") if selezionata else testo(lingua, "bottone_vota")
        titolo_escaped = html.escape(canzone['titolo'])
        artista_escaped = html.escape(canzone['artista']) or "&nbsp;"
        durata_escaped = html.escape(canzone['durata_testo'])
        id_escaped = html.escape(canzone['id'])
        righe.append(f"""
        <div class="canzone{classe_extra}">
          {immagine_html}
          <div class="info">
            <div class="titolo">{titolo_escaped}</div>
            <div class="artista">{artista_escaped}</div>
            <div class="durata">{durata_escaped}</div>
          </div>
          <form class="voto" method="post" action="/vota">
            <input type="hidden" name="id" value="{id_escaped}">
            <button type="submit">{etichetta_bottone}</button>
          </form>
        </div>
        """)

    avviso_html = ""
    if avviso == "rifiutato":
        avviso_html = f'<p class="avviso-pausa toast-notifica avviso-rifiutato" onclick="this.remove()">{testo(lingua, "avviso_in_pausa_rifiutato")}</p>'
    elif avviso == "accettato":
        avviso_html = f'<p class="avviso-pausa toast-notifica avviso-accettato" onclick="this.remove()">{testo(lingua, "avviso_in_pausa_accettato")}</p>'
    elif avviso == "coda_piena":
        avviso_html = f'<p class="avviso-pausa toast-notifica avviso-coda-piena" onclick="this.remove()">{testo(lingua, "avviso_coda_piena")}</p>'

    script_toast = ""
    if avviso_html:
        script_toast = """
<script>
(function(){
  var t = document.querySelector('.toast-notifica');
  if (!t) return;
  setTimeout(function(){
    t.classList.add('toast-nascosta');
    setTimeout(function(){ t.remove(); }, 400);
  }, 5000);
})();
</script>"""

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'titolo_pagina_voto')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/vota')}
{_bottone_secondario_html(testo(lingua, 'vi_bottone_indietro'), '/menu')}
<h1>{testo(lingua, 'intestazione_voto')}</h1>
<p style="text-align:center;color:#999">{testo(lingua, 'istruzioni_voto')}</p>
{avviso_html}
{''.join(righe)}
{script_toast}
</body></html>"""


def _paragrafi_benvenuto_html(testo_grezzo: str) -> str:
    """Impagina il testo libero della pagina di benvenuto in paragrafi
    distinti, ciascuno con la propria spaziatura: una riga vuota nel
    campo testo (invio doppio) separa due paragrafi, un invio singolo
    dentro lo stesso paragrafo resta un semplice a-capo. Cosi' si puo'
    scrivere ad es. un titolo su un paragrafo e un sottotitolo staccato
    sotto, senza bisogno di HTML."""
    paragrafi_html = []
    for paragrafo_grezzo in testo_grezzo.replace("\r\n", "\n").split("\n\n"):
        paragrafo_grezzo = paragrafo_grezzo.strip("\n")
        if not paragrafo_grezzo.strip():
            continue
        paragrafo_escaped = html.escape(paragrafo_grezzo).replace("\n", "<br>")
        paragrafi_html.append(f'<p class="paragrafo-benvenuto">{paragrafo_escaped}</p>')
    return "".join(paragrafi_html)


def pagina_benvenuto(lingua: str = LINGUA_DEFAULT) -> str:
    """Prima pagina che vedono gli ospiti (il Fritz!Box porta qui chi si
    connette al Wi-Fi ospiti): testo/immagine/audio a piacere, configurati
    da ManagerShow e letti in configurazione_benvenuto, con un pulsante che
    porta alla pagina di voto vera e propria (/vota).

    Layout a blocchi impilati (testo, immagine, pulsanti), MAI sovrapposti:
    testo e barra pulsanti hanno altezza fissa (mai schiacciati/rimpiccioliti,
    sempre leggibili per intero), l'immagine occupa lo spazio restante e non
    viene mai ritagliata (object-fit:contain mostra sempre l'immagine
    intera, con eventuali bande vuote riempite dello stesso colore della
    fascia testo). Se tutto insieme supera lo schermo, la pagina scorre
    invece di tagliare o sovrapporre qualcosa."""
    config = configurazione_benvenuto
    testo_html = _paragrafi_benvenuto_html(config.get("testo", ""))
    colore = html.escape(config.get("colore", "#1e1e1e")) or "#1e1e1e"
    immagine = config.get("immagine", "")
    audio = config.get("audio", "")

    colore_testo = _colore_testo_leggibile(colore)
    fascia_testo_html = (
        f'<div class="fascia-testo" style="background:{colore};color:{colore_testo}">{testo_html}</div>'
        if testo_html else ""
    )
    area_immagine_html = (
        f'<div class="area-immagine" style="background:{colore}">'
        f'<img class="immagine-benvenuto" src="/benvenuto/{html.escape(immagine)}" alt="">'
        f'</div>'
        if immagine else ""
    )
    audio_html = (
        f'<audio id="audio-benvenuto" src="/benvenuto/{html.escape(audio)}" preload="none"></audio>'
        if audio else ""
    )
    bottone_ascolta_html = (
        f'<button type="button" class="bottone-benvenuto secondario" onclick="ascoltaBenvenuto(this)">'
        f'{testo(lingua, "bv_bottone_ascolta")}</button>'
        if audio else ""
    )
    # Col saluto disabilitato non ha senso mostrare un bivio con una sola
    # strada valida: si salta dritti alla pagina-ponte del voto.
    percorso_continua = "/menu" if saluto_babbonatale_abilitato else "/vota-intro"

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'bv_titolo_pagina')}</title>
<style>
  html, body {{ margin:0; padding:0; background:#111; color:#eee;
                font-family: Arial, Helvetica, sans-serif; }}
  .schermo-benvenuto {{ min-height:100dvh; display:flex; flex-direction:column; }}
  .fascia-testo {{ flex:0 0 auto; padding:20px 16px; text-align:center; font-size:1.1em; }}
  .paragrafo-benvenuto {{ margin:0 0 12px; }}
  .paragrafo-benvenuto:last-child {{ margin-bottom:0; }}
  .area-immagine {{ flex:1 1 auto; min-height:160px; display:flex;
                    align-items:center; justify-content:center; overflow:hidden; }}
  .immagine-benvenuto {{ width:100%; height:100%; object-fit:contain; display:block; }}
  .barra-bottoni {{ flex:0 0 auto; display:flex; flex-wrap:wrap; justify-content:center;
                    gap:12px; padding:16px 16px calc(16px + env(safe-area-inset-bottom)); }}
  .bottone-benvenuto {{ background:#2e7d32; color:#fff; border:none; border-radius:8px;
                        padding:14px 22px; font-size:1.05em; cursor:pointer; }}
  .bottone-benvenuto.secondario {{ background:#333; }}
  .selettore-lingua {{ text-align:center; padding-top:10px; }}
  .selettore-lingua a {{ color:#ccc; text-decoration:none; padding:4px 8px; }}
  .selettore-lingua a.attiva {{ color:#fff; font-weight:bold; text-decoration:underline; }}
</style>
</head><body>
{_selettore_lingua_html(lingua, '/')}
<div class="schermo-benvenuto">
  {fascia_testo_html}
  {area_immagine_html}
  <div class="barra-bottoni">
    {bottone_ascolta_html}
    <a class="bottone-benvenuto" href="{percorso_continua}">{testo(lingua, 'bv_bottone_vota')}</a>
  </div>
</div>
{audio_html}
<script>
function ascoltaBenvenuto(bottone) {{
  var audio = document.getElementById('audio-benvenuto');
  if (!audio) return;
  if (audio.paused) {{
    audio.play();
    bottone.textContent = "{testo(lingua, 'bv_bottone_pausa')}";
  }} else {{
    audio.pause();
    bottone.textContent = "{testo(lingua, 'bv_bottone_ascolta')}";
  }}
}}
</script>
</body></html>"""


def _info_configurata() -> bool:
    """True solo se la spunta "Visibile" e' attiva in ManagerShow - unico
    interruttore che conta, indipendente dal fatto che i campi sotto siano
    gia' compilati o no (se spuntata ma vuota, pagina_info mostra un
    placeholder generico invece di sparire)."""
    return bool(configurazione_info.get("visibile"))


def pagina_info(lingua: str = LINGUA_DEFAULT) -> str:
    """Pagina di contenuto libero (regole/sponsor/beneficenza/ecc.),
    raggiungibile da un link su /menu e /vota-intro - a differenza della
    pagina di benvenuto (vista una volta sola all'ingresso), questa resta
    riconsultabile in qualsiasi momento. Stessa identica struttura di
    pagina_benvenuto (testo/immagine/audio/colore), solo il pulsante in
    fondo cambia (torna al menu invece di continuare)."""
    config = configurazione_info
    # Testo vuoto = pagina abilitata (Visibile) ma non ancora configurata:
    # meglio un placeholder chiaro per gli ospiti che una pagina bianca.
    testo_html = _paragrafi_benvenuto_html(config.get("testo", "")) or (
        f'<p class="paragrafo-benvenuto">{html.escape(testo(lingua, "info_testo_placeholder"))}</p>'
    )
    colore = html.escape(config.get("colore", "#1e1e1e")) or "#1e1e1e"
    immagine = config.get("immagine", "")
    audio = config.get("audio", "")
    link_donazione = config.get("link_donazione", "")

    colore_testo = _colore_testo_leggibile(colore)
    fascia_testo_html = f'<div class="fascia-testo" style="background:{colore};color:{colore_testo}">{testo_html}</div>'
    area_immagine_html = (
        f'<div class="area-immagine" style="background:{colore}">'
        f'<img class="immagine-benvenuto" src="/info/{html.escape(immagine)}" alt="">'
        f'</div>'
        if immagine else ""
    )
    audio_html = (
        f'<audio id="audio-benvenuto" src="/info/{html.escape(audio)}" preload="none"></audio>'
        if audio else ""
    )
    bottone_ascolta_html = (
        f'<button type="button" class="bottone-benvenuto secondario" onclick="ascoltaBenvenuto(this)">'
        f'{testo(lingua, "bv_bottone_ascolta")}</button>'
        if audio else ""
    )
    bottone_dona_html = (
        f'<a class="bottone-benvenuto" href="{html.escape(link_donazione)}" target="_blank" rel="noopener">'
        f'{testo(lingua, "info_bottone_dona")}</a>'
        if link_donazione else ""
    )

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'info_titolo_pagina')}</title>
<style>
  html, body {{ margin:0; padding:0; background:#111; color:#eee;
                font-family: Arial, Helvetica, sans-serif; }}
  .schermo-benvenuto {{ min-height:100dvh; display:flex; flex-direction:column; }}
  .fascia-testo {{ flex:0 0 auto; padding:20px 16px; text-align:center; font-size:1.1em; }}
  .paragrafo-benvenuto {{ margin:0 0 12px; }}
  .paragrafo-benvenuto:last-child {{ margin-bottom:0; }}
  .area-immagine {{ flex:1 1 auto; min-height:160px; display:flex;
                    align-items:center; justify-content:center; overflow:hidden; }}
  .immagine-benvenuto {{ width:100%; height:100%; object-fit:contain; display:block; }}
  .barra-bottoni {{ flex:0 0 auto; display:flex; flex-wrap:wrap; justify-content:center;
                    gap:12px; padding:16px 16px calc(16px + env(safe-area-inset-bottom)); }}
  .bottone-benvenuto {{ background:#2e7d32; color:#fff; border:none; border-radius:8px;
                        padding:14px 22px; font-size:1.05em; cursor:pointer; text-decoration:none; display:inline-block; }}
  .bottone-benvenuto.secondario {{ background:#333; }}
  .selettore-lingua {{ text-align:center; padding-top:10px; }}
  .selettore-lingua a {{ color:#ccc; text-decoration:none; padding:4px 8px; }}
  .selettore-lingua a.attiva {{ color:#fff; font-weight:bold; text-decoration:underline; }}
</style>
</head><body>
{_selettore_lingua_html(lingua, '/info')}
<div class="schermo-benvenuto">
  {fascia_testo_html}
  {area_immagine_html}
  <div class="barra-bottoni">
    {bottone_dona_html}
    {bottone_ascolta_html}
    <a class="bottone-benvenuto secondario" href="/menu">{testo(lingua, 'vi_bottone_indietro')}</a>
  </div>
</div>
{audio_html}
<script>
function ascoltaBenvenuto(bottone) {{
  var audio = document.getElementById('audio-benvenuto');
  if (!audio) return;
  if (audio.paused) {{
    audio.play();
    bottone.textContent = "{testo(lingua, 'bv_bottone_pausa')}";
  }} else {{
    audio.pause();
    bottone.textContent = "{testo(lingua, 'bv_bottone_ascolta')}";
  }}
}}
</script>
</body></html>"""


def pagina_grazie(lingua: str = LINGUA_DEFAULT) -> str:
    """Pagina di ringraziamento post-donazione, indirizzo /grazie - pensata
    come indirizzo di ritorno da impostare nel servizio di pagamento
    scelto (Stripe Checkout/PayPal lo supportano entrambi). Segue la
    stessa spunta "Visibile" di /info (vedi _info_configurata), nessun
    interruttore separato. Testo_Grazie vuoto = messaggio di default gia'
    pronto, stesso colore della pagina Beneficenza, niente immagine/audio
    propri (struttura minima, si riusa il colore configurato per Info)."""
    config = configurazione_info
    testo_grazie = config.get("testo_grazie", "").strip()
    testo_html = (
        _paragrafi_benvenuto_html(testo_grazie) if testo_grazie
        else f'<p class="paragrafo-benvenuto">{html.escape(testo(lingua, "grazie_testo_default"))}</p>'
    )
    colore = html.escape(config.get("colore", "#1e1e1e")) or "#1e1e1e"
    colore_testo = _colore_testo_leggibile(colore)

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'grazie_titolo_pagina')}</title>
<style>
  html, body {{ margin:0; padding:0; background:#111; color:#eee;
                font-family: Arial, Helvetica, sans-serif; }}
  .schermo-benvenuto {{ min-height:100dvh; display:flex; flex-direction:column; }}
  .fascia-testo {{ flex:1 1 auto; padding:20px 16px; text-align:center; font-size:1.3em;
                   display:flex; align-items:center; justify-content:center; }}
  .paragrafo-benvenuto {{ margin:0 0 12px; }}
  .paragrafo-benvenuto:last-child {{ margin-bottom:0; }}
  .barra-bottoni {{ flex:0 0 auto; display:flex; flex-wrap:wrap; justify-content:center;
                    gap:12px; padding:16px 16px calc(16px + env(safe-area-inset-bottom)); }}
  .bottone-benvenuto {{ background:#2e7d32; color:#fff; border:none; border-radius:8px;
                        padding:14px 22px; font-size:1.05em; cursor:pointer; text-decoration:none; display:inline-block; }}
  .selettore-lingua {{ text-align:center; padding-top:10px; }}
  .selettore-lingua a {{ color:#ccc; text-decoration:none; padding:4px 8px; }}
  .selettore-lingua a.attiva {{ color:#fff; font-weight:bold; text-decoration:underline; }}
</style>
</head><body>
{_selettore_lingua_html(lingua, '/grazie')}
<div class="schermo-benvenuto">
  <div class="fascia-testo" style="background:{colore};color:{colore_testo}">{testo_html}</div>
  <div class="barra-bottoni">
    <a class="bottone-benvenuto" href="/menu">{testo(lingua, 'vi_bottone_indietro')}</a>
  </div>
</div>
</body></html>"""


def _righe_canzoni_html(voci: list, lingua: str, con_barra: bool, id_in_pausa: set = None) -> str:
    id_in_pausa = id_in_pausa or set()
    righe = []
    for voce in voci:
        titolo_escaped = html.escape(voce['titolo'])
        artista_escaped = html.escape(voce['artista']) or "&nbsp;"
        etichetta_pausa_html = (
            f' <span class="etichetta-in-pausa">({testo(lingua, "etichetta_in_pausa")})</span>'
            if voce.get("id") in id_in_pausa else ""
        )
        barra_html = (
            f"""<div class="barra"><div class="barra-interna" style="width:{voce['percentuale']}%"></div></div>
            <div class="percentuale">{voce['percentuale']}%</div>"""
            if con_barra else ""
        )
        righe.append(f"""
        <div class="canzone">
          <div class="info">
            <div class="titolo">{titolo_escaped} - {voce['voti']} {testo(lingua, 'voti_suffisso')}{etichetta_pausa_html}</div>
            <div class="artista">{artista_escaped}</div>
            {barra_html}
          </div>
        </div>
        """)
    return ''.join(righe)


def _nome_modalita_coda_html(lingua: str) -> str:
    """Nome leggibile della modalita' di coda attiva in questo momento
    (modalita_coda_attiva/tetto_coda_attivo, letti una volta all'avvio -
    vedi avvia_server), da mostrare in pagina_risultati()."""
    if modalita_coda_attiva == "turno":
        return testo(lingua, "modalita_coda_nome_turno")
    if modalita_coda_attiva == "tetto":
        return testo(lingua, "modalita_coda_nome_tetto", tetto=tetto_coda_attivo)
    return testo(lingua, "modalita_coda_nome_persistente")


def _nota_coda_html(lingua: str) -> str:
    """Nota della colonna 'in coda', diversa a seconda della modalita'
    attiva: senza questo la nota potrebbe descrivere un comportamento
    (es. 'niente viene mai scartato') che nella modalita' scelta in quel
    momento non e' vero, confondendo chi guarda la pagina."""
    if modalita_coda_attiva == "turno":
        return testo(lingua, "colonna_coda_salvata_nota_turno")
    if modalita_coda_attiva == "tetto":
        return testo(lingua, "colonna_coda_salvata_nota_tetto", tetto=tetto_coda_attivo)
    return testo(lingua, "colonna_coda_salvata_nota_persistente")


def pagina_risultati(lingua: str = LINGUA_DEFAULT) -> str:
    classifica = calcola_classifica()
    totale_voti = sum(v["voti"] for v in classifica)
    classifica_votate = [v for v in classifica if v["voti"] > 0]
    corpo_live = _righe_canzoni_html(classifica_votate, lingua, con_barra=True) or f"<p>{testo(lingua, 'nessun_voto')}</p>"

    vincente = classifica_vincente_condivisa
    # Il primo elemento di "vincente" e' quello appena mandato in playNext
    # (in onda ora): viene registrato nel raffreddamento nello stesso
    # istante (per non farlo rivincere subito), quindi risulterebbe lui
    # stesso "in pausa" nell'insieme condiviso - etichetta sbagliata per
    # una voce che sta letteralmente suonando. Si esclude solo lui, il
    # resto della coda (dietro di lui) mantiene l'etichetta se davvero
    # ancora in raffreddamento.
    id_in_pausa_vincente = (
        id_in_pausa_condiviso - {vincente[0]["id"]} if vincente else id_in_pausa_condiviso
    )
    corpo_vincente = (
        _righe_canzoni_html(vincente, lingua, con_barra=False, id_in_pausa=id_in_pausa_vincente)
        if vincente else f"<p>{testo(lingua, 'nessuna_vincente')}</p>"
    )

    coda = coda_classifica_condivisa
    corpo_coda = (
        _righe_canzoni_html(coda, lingua, con_barra=False, id_in_pausa=id_in_pausa_condiviso)
        if coda else f"<p>{testo(lingua, 'coda_vuota')}</p>"
    )

    # In modalita' "persistente" la colonna centrale mostra gia' vincitore
    # attuale + resto della coda insieme: ripeterla identica (meno il
    # primo elemento) nella terza colonna e' pura ridondanza, si toglie.
    colonna_coda_html = "" if modalita_coda_attiva == "persistente" else f"""
  <div class="colonna">
    <h2>{testo(lingua, 'colonna_coda_salvata')}</h2>
    <p class="nota-colonna">{_nota_coda_html(lingua)}</p>
    {corpo_coda}
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'titolo_pagina_risultati')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/risultati')}
<h1>{testo(lingua, 'intestazione_risultati')} - {totale_voti} {testo(lingua, 'voti_totali')}</h1>
<p style="text-align:center;color:#999">{testo(lingua, 'modalita_coda_label')} <strong>{_nome_modalita_coda_html(lingua)}</strong></p>
<div class="tre-colonne">
  <div class="colonna">
    <h2>{testo(lingua, 'colonna_classifica_live')}</h2>
    {corpo_live}
  </div>
  <div class="colonna">
    <h2>{testo(lingua, 'colonna_vincente')}</h2>
    <p class="nota-colonna">{testo(lingua, 'colonna_vincente_nota')}</p>
    {corpo_vincente}
  </div>{colonna_coda_html}
</div>
<form class="reset" method="post" action="/reset" onsubmit="return confirm('{testo(lingua, 'conferma_reset')}');">
  <button type="submit">{testo(lingua, 'bottone_reset')}</button>
</form>
</body></html>"""


def pagina_login(lingua: str = LINGUA_DEFAULT, errore: bool = False) -> str:
    messaggio_errore = f'<p style="color:#e57373">{testo(lingua, "pagina_login_errore")}</p>' if errore else ""
    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'pagina_login_titolo')}</title>
{STILE_BASE}
</head><body>
<h1>{testo(lingua, 'pagina_login_titolo')}</h1>
<p style="text-align:center;color:#999">{testo(lingua, 'pagina_login_istruzioni')}</p>
{messaggio_errore}
<form method="post" action="/accedi" style="max-width:280px;margin:0 auto;text-align:center">
  <input type="password" name="token" autofocus style="width:100%;padding:10px;margin-bottom:10px;box-sizing:border-box">
  <button type="submit">{testo(lingua, 'pagina_login_bottone')}</button>
</form>
</body></html>"""


def _bottone_grande_html(testo_bottone: str, href: str) -> str:
    return (
        f'<a href="{href}" style="display:block;background:#2e7d32;color:#fff;'
        f'text-decoration:none;text-align:center;border-radius:8px;padding:16px;'
        f'font-size:1.05em">{testo_bottone}</a>'
    )


def _bottone_secondario_html(testo_bottone: str, href: str) -> str:
    return (
        f'<a href="{href}" style="display:block;color:#ffc107;font-weight:bold;'
        f'text-decoration:none;text-align:center;padding:8px">{testo_bottone}</a>'
    )


def pagina_menu(lingua: str = LINGUA_DEFAULT) -> str:
    """Bivio mostrato dopo la pagina di benvenuto: il genitore sceglie se
    far salutare il figlio da Babbo Natale, votare l'animazione preferita,
    o entrambe le cose (nessuna scelta esclude l'altra, si torna sempre
    qui dopo)."""
    link_info_html = (
        _bottone_secondario_html(testo(lingua, 'hub_link_info'), '/info') if _info_configurata() else ""
    )
    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'hub_titolo_pagina')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/menu')}
<h1>{testo(lingua, 'hub_titolo_pagina')}</h1>
<p style="text-align:center;color:#999">{testo(lingua, 'hub_intro')}</p>
<div style="max-width:360px;margin:24px auto;display:flex;flex-direction:column;gap:14px">
  {_bottone_grande_html(testo(lingua, 'hub_bottone_saluto'), '/saluto')}
  {_bottone_grande_html(testo(lingua, 'hub_bottone_vota'), '/vota-intro')}
</div>
{link_info_html}
</body></html>"""


def pagina_vota_intro(lingua: str = LINGUA_DEFAULT) -> str:
    """Pagina-ponte prima di /vota: spiega in breve che si tratta di un
    voto pubblico (non una richiesta garantita immediata), cosi' nessuno
    arriva alla pagina di voto con aspettative sbagliate. La nota sulla
    coda riusa _nome_modalita_coda_html/_nota_coda_html (le stesse di
    pagina_risultati): deve restare coerente con la modalita' scelta in
    ManagerShow, non descrivere un comportamento che magari non e' quello
    attivo in questo momento."""
    nota_coda_html = f"""
<p style="text-align:center;color:#999;font-size:0.9em;max-width:480px;margin:0 auto 20px">
  <strong>{testo(lingua, 'modalita_coda_label')}</strong> {_nome_modalita_coda_html(lingua)}<br>
  {_nota_coda_html(lingua)}
</p>"""
    link_info_html = (
        _bottone_secondario_html(testo(lingua, 'hub_link_info'), '/info') if _info_configurata() else ""
    )
    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'vi_titolo_pagina')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/vota-intro')}
<h1>{testo(lingua, 'vi_titolo_pagina')}</h1>
<p style="text-align:center;max-width:480px;margin:0 auto 20px">{testo(lingua, 'vi_testo')}</p>
{nota_coda_html}
<div style="max-width:360px;margin:0 auto;display:flex;flex-direction:column;gap:10px">
  {_bottone_grande_html(testo(lingua, 'vi_bottone_continua'), '/vota')}
  {_bottone_secondario_html(testo(lingua, 'vi_bottone_indietro'), '/menu')}
</div>
{link_info_html}
</body></html>"""


def pagina_saluto_intro(lingua: str = LINGUA_DEFAULT) -> str:
    """Pagina-ponte prima di /saluto/nome: spiega il funzionamento del
    saluto personalizzato e avvisa che, essendo generato al momento da un
    computer dedicato, possono capitare rari intoppi tecnici non dipendenti
    dall'organizzazione - per gestire le aspettative prima di scrivere il
    nome, non dopo."""
    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'sg_titolo_pagina')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/saluto')}
<h1>{testo(lingua, 'sg_titolo_pagina')}</h1>
<p style="text-align:center;max-width:480px;margin:0 auto 20px">{testo(lingua, 'sg_testo')}</p>
<div style="max-width:360px;margin:0 auto;display:flex;flex-direction:column;gap:10px">
  {_bottone_grande_html(testo(lingua, 'sg_bottone_continua'), '/saluto/nome')}
  {_bottone_secondario_html(testo(lingua, 'sg_bottone_indietro'), '/menu')}
</div>
</body></html>"""


def pagina_saluto_nome(lingua: str = LINGUA_DEFAULT, fatto: bool = False, nome: str = "") -> str:
    """Raccoglie il nome del bambino da salutare. PLACEHOLDER: per ora non
    genera ne' salva nulla (la pipeline scroll+audio non esiste ancora),
    mostra solo una conferma - pronta da collegare in seguito senza dover
    toccare la navigazione. Supporta piu' figli: dopo la conferma si puo'
    tornare subito al form per un altro nome, invece di dover rifare tutto
    il percorso dal menu."""
    if fatto:
        nome_escaped = html.escape(nome) or "?"
        corpo_html = f"""
<h1>{testo(lingua, 'sn_titolo_pagina')}</h1>
<p style="text-align:center">{testo(lingua, 'sn_confermato_testo', nome=nome_escaped)}</p>
<p style="text-align:center;color:#999;font-size:0.9em">{testo(lingua, 'sn_confermato_nota')}</p>
<div style="max-width:360px;margin:20px auto;display:flex;flex-direction:column;gap:10px">
  {_bottone_grande_html(testo(lingua, 'sn_bottone_aggiungi_altro'), '/saluto/nome')}
  {_bottone_grande_html(testo(lingua, 'sn_bottone_vai_voto'), '/vota-intro')}
  {_bottone_secondario_html(testo(lingua, 'sn_bottone_torna_menu'), '/menu')}
</div>"""
    else:
        corpo_html = f"""
<h1>{testo(lingua, 'sn_titolo_pagina')}</h1>
<form method="post" action="/saluto/nome" style="max-width:320px;margin:20px auto;text-align:center">
  <label style="display:block;margin-bottom:8px;color:#ccc">{testo(lingua, 'sn_etichetta_nome')}</label>
  <input type="text" name="nome" maxlength="40" required autofocus
         style="width:100%;padding:10px;margin-bottom:14px;box-sizing:border-box;
                border-radius:6px;border:none;font-size:1.05em">
  <button type="submit" style="background:#2e7d32;color:#fff;border:none;border-radius:8px;
          padding:14px 22px;font-size:1.05em;width:100%">{testo(lingua, 'sn_bottone_invia')}</button>
</form>
<div style="max-width:320px;margin:0 auto">
  {_bottone_secondario_html(testo(lingua, 'sg_bottone_indietro'), '/menu')}
</div>"""

    return f"""<!DOCTYPE html>
<html lang="{lingua}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{testo(lingua, 'sn_titolo_pagina')}</title>
{STILE_BASE}
</head><body>
{_selettore_lingua_html(lingua, '/saluto/nome')}
{corpo_html}
</body></html>"""


# ----------------------------------------------------------------------
# Server HTTP
# ----------------------------------------------------------------------
class GestoreRichieste(BaseHTTPRequestHandler):
    logger: logging.Logger = None  # impostato in avvia_server()
    token_admin: str = ""  # impostato in avvia_server(); vuoto = nessuna protezione
    timeout = TIMEOUT_SOCKET_SECONDI  # chiude chi apre una connessione e non manda dati

    def log_message(self, formato, *args):
        pass  # evitiamo il log di default sulla console, usiamo self.logger

    def _leggi_cookie(self, nome: str) -> str:
        intestazione_cookie = self.headers.get("Cookie")
        if intestazione_cookie:
            jar = cookies.SimpleCookie()
            jar.load(intestazione_cookie)
            if nome in jar:
                return jar[nome].value
        return ""

    def _leggi_o_crea_id_elettore(self) -> str:
        return self._leggi_cookie(NOME_COOKIE_ELETTORE) or uuid.uuid4().hex

    def _leggi_lingua(self) -> str:
        lingua = self._leggi_cookie(NOME_COOKIE_LINGUA)
        return lingua if lingua in LINGUE_VALIDE else LINGUA_DEFAULT

    def _autenticato(self) -> bool:
        if not self.token_admin:
            return True  # protezione disattivata (token vuoto in configurazione)
        return self._leggi_cookie(NOME_COOKIE_ADMIN) == self.token_admin

    def _invia_html(self, corpo: str, id_elettore: str = None, codice: int = 200):
        dati = corpo.encode("utf-8")
        self.send_response(codice)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(dati)))
        # Le pagine mostrano stato che cambia in continuazione (voti,
        # coda, modalita' impostata da ManagerShow): senza questo header
        # alcuni browser possono servire una copia vecchia dalla cache
        # invece di richiederla di nuovo al server, anche col refresh
        # automatico o riaprendo la pagina/scheda.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        if id_elettore:
            self.send_header(
                "Set-Cookie", f"{NOME_COOKIE_ELETTORE}={id_elettore}; Path=/; Max-Age=2592000"
            )
        self.end_headers()
        self.wfile.write(dati)

    def _invia_redirect(self, percorso: str, id_elettore: str = None, lingua: str = None, token_admin: str = None):
        self.send_response(303)
        self.send_header("Location", percorso)
        if id_elettore:
            self.send_header(
                "Set-Cookie", f"{NOME_COOKIE_ELETTORE}={id_elettore}; Path=/; Max-Age=2592000"
            )
        if lingua:
            self.send_header(
                "Set-Cookie", f"{NOME_COOKIE_LINGUA}={lingua}; Path=/; Max-Age=2592000"
            )
        if token_admin:
            self.send_header(
                "Set-Cookie", f"{NOME_COOKIE_ADMIN}={token_admin}; Path=/; Max-Age=43200"
            )
        self.end_headers()

    def _servi_copertina(self, percorso: str):
        nome_file = percorso.removeprefix("/copertine/")
        file_immagine = (CARTELLA_COPERTINE / nome_file).resolve()

        if CARTELLA_COPERTINE not in file_immagine.parents or not file_immagine.is_file():
            self.send_response(404)
            self.end_headers()
            return

        estensione = file_immagine.suffix.lower()
        content_type = "image/png" if estensione == ".png" else "image/jpeg"
        dati = file_immagine.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(dati)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(dati)

    def _servi_file_benvenuto(self, percorso: str):
        """Serve l'immagine/audio della pagina di benvenuto, configurati da
        ManagerShow e salvati in CARTELLA_BENVENUTO."""
        nome_file = percorso.removeprefix("/benvenuto/")
        file_richiesto = (CARTELLA_BENVENUTO / nome_file).resolve()

        if CARTELLA_BENVENUTO not in file_richiesto.parents or not file_richiesto.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type_per_estensione = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".mp3": "audio/mpeg",
        }
        content_type = content_type_per_estensione.get(file_richiesto.suffix.lower(), "application/octet-stream")
        dati = file_richiesto.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(dati)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(dati)

    def _servi_file_info(self, percorso: str):
        """Serve l'immagine/audio della pagina Info, configurati da
        ManagerShow e salvati in CARTELLA_INFO - stessa logica di
        _servi_file_benvenuto, cartella separata."""
        nome_file = percorso.removeprefix("/info/")
        file_richiesto = (CARTELLA_INFO / nome_file).resolve()

        if CARTELLA_INFO not in file_richiesto.parents or not file_richiesto.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type_per_estensione = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".mp3": "audio/mpeg",
        }
        content_type = content_type_per_estensione.get(file_richiesto.suffix.lower(), "application/octet-stream")
        dati = file_richiesto.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(dati)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(dati)

    def _richiesta_troppo_frequente(self) -> bool:
        """True se questo IP ha gia' fatto una richiesta POST negli ultimi
        INTERVALLO_MINIMO_RICHIESTA_SECONDI secondi (anti-spam/flood).
        Il loopback (127.0.0.1/::1) e' sempre esente: serve per aprire piu'
        finestre di test dallo stesso PC (vedi 'Test multi-voto' nella GUI)
        senza farsi bloccare a vicenda. Un ospite vero non puo' MAI
        comparire come loopback al server (ci arriva sempre con l'IP di
        rete), quindi l'esenzione non e' sfruttabile dall'esterno."""
        ip_client = self.client_address[0]
        if ip_client in ("127.0.0.1", "::1"):
            return False
        ora = time.monotonic()
        with _lock_rate_limit:
            ultima = _ultima_richiesta_per_ip.get(ip_client)
            if ultima is not None and ora - ultima < INTERVALLO_MINIMO_RICHIESTA_SECONDI:
                return True
            _ultima_richiesta_per_ip[ip_client] = ora
            return False

    def _gestisci_cambio_lingua(self, query: dict):
        lingua_scelta = query.get("imposta", [LINGUA_DEFAULT])[0]
        if lingua_scelta not in LINGUE_VALIDE:
            lingua_scelta = LINGUA_DEFAULT

        percorso_ritorno = query.get("torna", ["/"])[0]
        if not percorso_ritorno.startswith("/"):
            percorso_ritorno = "/"  # mai reindirizzare fuori dal nostro sito

        self._invia_redirect(percorso_ritorno, lingua=lingua_scelta)

    def do_GET(self):
        try:
            parti_url = urlsplit(self.path)
            percorso = parti_url.path
            query = parse_qs(parti_url.query)

            if percorso == "/" or percorso == "":
                self._invia_html(pagina_benvenuto(self._leggi_lingua()))
            elif percorso == "/menu":
                if saluto_babbonatale_abilitato:
                    self._invia_html(pagina_menu(self._leggi_lingua()))
                else:
                    self._invia_redirect("/vota-intro")
            elif percorso == "/vota-intro":
                self._invia_html(pagina_vota_intro(self._leggi_lingua()))
            elif percorso == "/saluto":
                if saluto_babbonatale_abilitato:
                    self._invia_html(pagina_saluto_intro(self._leggi_lingua()))
                else:
                    self._invia_redirect("/vota-intro")
            elif percorso == "/saluto/nome":
                if not saluto_babbonatale_abilitato:
                    self._invia_redirect("/vota-intro")
                else:
                    lingua = self._leggi_lingua()
                    fatto = query.get("fatto", [""])[0] == "1"
                    nome_confermato = query.get("nome", [""])[0]
                    self._invia_html(pagina_saluto_nome(lingua, fatto=fatto, nome=nome_confermato))
            elif percorso == "/vota":
                id_elettore = self._leggi_o_crea_id_elettore()
                lingua = self._leggi_lingua()
                avviso = query.get("avviso", [""])[0]
                self._invia_html(pagina_voto(id_elettore, lingua, avviso=avviso), id_elettore=id_elettore)
            elif percorso == "/risultati":
                token_query = query.get("token", [None])[0]
                if token_query and self.token_admin and token_query == self.token_admin:
                    # arrivo da un link/pulsante con la password gia' nell'url
                    # (es. il pulsante nella GUI): autentica e ripulisce l'url
                    self._invia_redirect("/risultati", token_admin=self.token_admin)
                elif self._autenticato():
                    self._invia_html(pagina_risultati(self._leggi_lingua()))
                else:
                    self._invia_html(pagina_login(self._leggi_lingua()), codice=401)
            elif percorso == "/lingua":
                self._gestisci_cambio_lingua(query)
            elif percorso.startswith("/copertine/"):
                self._servi_copertina(percorso)
            elif percorso.startswith("/benvenuto/"):
                self._servi_file_benvenuto(percorso)
            elif percorso == "/info":
                if _info_configurata():
                    self._invia_html(pagina_info(self._leggi_lingua()))
                else:
                    self._invia_redirect("/menu")
            elif percorso.startswith("/info/"):
                self._servi_file_info(percorso)
            elif percorso == "/grazie":
                if _info_configurata():
                    self._invia_html(pagina_grazie(self._leggi_lingua()))
                else:
                    self._invia_redirect("/menu")
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as errore:
            self.logger.error("Errore gestendo GET %s: %s", self.path, errore)
            self.send_response(500)
            self.end_headers()

    def do_POST(self):
        if self._richiesta_troppo_frequente():
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.end_headers()
            return
        try:
            try:
                lunghezza = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                lunghezza = 0  # header Content-Length corrotto/assente: corpo vuoto invece di crashare

            if lunghezza > MAX_LUNGHEZZA_CORPO_POST:
                # corpo anomalo per una pagina che manda solo un id o una password
                # corti: rifiutiamo senza leggerlo (evita di restare bloccati a
                # ricevere un payload enorme) e chiudiamo la connessione.
                self.logger.warning(
                    "POST %s rifiutato: corpo troppo grande (%d byte)", self.path, lunghezza
                )
                self.send_response(413)
                self.end_headers()
                self.close_connection = True
                return

            corpo_grezzo = self.rfile.read(lunghezza) if lunghezza else b""
            corpo = corpo_grezzo.decode("utf-8", errors="replace")
            campi = parse_qs(corpo)

            if self.path == "/saluto/nome":
                if not saluto_babbonatale_abilitato:
                    self._invia_redirect("/vota-intro")
                else:
                    # PLACEHOLDER: nessuna generazione/salvataggio ancora,
                    # vedi pagina_saluto_nome - qui si limita a validare e
                    # passare il nome alla pagina di conferma via redirect.
                    nome_grezzo = _sanifica_testo_pubblico(campi.get("nome", [""])[0]).strip()[:40]
                    if nome_grezzo:
                        self._invia_redirect(f"/saluto/nome?fatto=1&nome={quote(nome_grezzo)}")
                    else:
                        self._invia_redirect("/saluto/nome")

            elif self.path == "/vota":
                id_elettore = self._leggi_o_crea_id_elettore()
                id_canzone = _sanifica_testo_pubblico(campi.get("id", [""])[0])
                id_validi = {c["id"] for c in catalogo_canzoni}
                parametro_avviso = ""
                if id_canzone in id_validi:
                    in_pausa = id_canzone in id_in_pausa_condiviso
                    # In modalita' "turno" non c'e' nessuna coda: un voto
                    # per una sequenza in pausa non potrebbe MAI servire a
                    # niente (non puo' vincere questo turno, e in questa
                    # modalita' non esiste un "resto" che sopravviva al
                    # turno) - si rifiuta, invece di far credere che il
                    # voto conti per qualcosa. Nelle altre modalita' il
                    # resto della classifica finisce comunque in coda,
                    # quindi il voto ha senso: si accetta.
                    rifiutato = in_pausa and modalita_coda_attiva == "turno"

                    if not rifiutato:
                        is_test = self.client_address[0] in ("127.0.0.1", "::1")
                        with _lock_voti:
                            voti_correnti[id_elettore] = (id_canzone, time.time(), is_test)
                        salva_stato_voti(self.logger)
                        canzone_votata = next((c for c in catalogo_canzoni if c["id"] == id_canzone), None)
                        _registra_voto_db(
                            self.logger, id_canzone,
                            canzone_votata["titolo"] if canzone_votata else id_canzone,
                            id_elettore, is_test,
                        )
                        self.logger.info("Voto registrato: elettore=%s canzone=%s", id_elettore, id_canzone)
                    else:
                        self.logger.info(
                            "Voto rifiutato (sequenza in pausa, modalita' 'turno'): elettore=%s canzone=%s",
                            id_elettore, id_canzone,
                        )

                    if in_pausa:
                        parametro_avviso = "?avviso=rifiutato" if rifiutato else "?avviso=accettato"
                    elif (
                        not rifiutato
                        and modalita_coda_attiva == "tetto"
                        and len(coda_classifica_condivisa) >= tetto_coda_attivo
                        and id_canzone not in {v["id"] for v in coda_classifica_condivisa}
                        and not (classifica_vincente_condivisa and classifica_vincente_condivisa[0]["id"] == id_canzone)
                    ):
                        # La coda e' gia' al tetto massimo e questa canzone
                        # non ci sta ancora dentro (ne' e' quella in onda
                        # ora): se vince questo turno rischia di non
                        # trovare posto (vedi ponte_lor, "tetto" pieno). Non
                        # possiamo saperlo con certezza adesso (dipende da
                        # chi altro vince il turno), ma e' giusto avvisare
                        # subito chi vota invece di scoprirlo solo dopo.
                        parametro_avviso = "?avviso=coda_piena"
                else:
                    self.logger.warning("Voto ignorato, id canzone non valido: '%s'", id_canzone)
                self._invia_redirect(f"/vota{parametro_avviso}", id_elettore=id_elettore)

            elif self.path == "/reset":
                if not self._autenticato():
                    self.send_response(403)
                    self.end_headers()
                    return
                with _lock_voti:
                    voti_correnti.clear()
                salva_stato_voti(self.logger)
                self.logger.info("Voti azzerati: nuovo turno iniziato")
                self._invia_redirect("/risultati")

            elif self.path == "/accedi":
                token_inserito = campi.get("token", [""])[0]
                if self.token_admin and token_inserito == self.token_admin:
                    self.logger.info("Accesso amministratore riuscito")
                    self._invia_redirect("/risultati", token_admin=self.token_admin)
                else:
                    self.logger.warning("Tentativo di accesso amministratore con password errata")
                    self._invia_html(pagina_login(self._leggi_lingua(), errore=True), codice=401)

            else:
                self.send_response(404)
                self.end_headers()
        except Exception as errore:
            self.logger.error("Errore gestendo POST %s: %s", self.path, errore)
            self.send_response(500)
            self.end_headers()


class ServerVoto(ThreadingHTTPServer):
    """ThreadingHTTPServer con un tetto al numero di connessioni gestite in
    contemporanea. Ogni connessione fa partire un thread: senza un limite,
    un flood di connessioni (anche senza mandare dati validi, quindi prima
    che scatti il rate limiting per IP) potrebbe consumare memoria/CPU del
    PC dello show - lo stesso PC che deve restare libero per pilotare le
    luci. Oltre il tetto, le connessioni in piu' vengono chiuse subito
    senza aprire un thread."""
    daemon_threads = True
    logger: logging.Logger = None  # impostato in avvia_server()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._semaforo_connessioni = threading.BoundedSemaphore(MAX_CONNESSIONI_CONTEMPORANEE)

    def process_request(self, request, client_address):
        if not self._semaforo_connessioni.acquire(blocking=False):
            if self.logger:
                self.logger.warning(
                    "Troppe connessioni in contemporanea (>%d): connessione da %s rifiutata subito",
                    MAX_CONNESSIONI_CONTEMPORANEE, client_address[0],
                )
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._semaforo_connessioni.release()


def avvia_server():
    logger = configura_logging()
    logger.info("=== Avvio VotoShow ===")

    global catalogo_canzoni, configurazione_benvenuto, configurazione_info, saluto_babbonatale_abilitato
    catalogo_canzoni = carica_catalogo(logger)
    carica_stato_voti_precedente(logger)
    inizializza_database_storico(logger)

    saluto_babbonatale_abilitato = leggi_saluto_babbonatale(logger)
    logger.info(
        "[Saluto Babbo Natale] %s",
        "ABILITATO" if saluto_babbonatale_abilitato else "disabilitato (Abilita = False o sezione assente)",
    )

    configurazione_benvenuto = leggi_configurazione_benvenuto(logger)
    logger.info(
        "[Benvenuto] testo=%s immagine='%s' audio='%s'",
        "presente" if configurazione_benvenuto["testo"] else "assente",
        configurazione_benvenuto["immagine"], configurazione_benvenuto["audio"],
    )

    configurazione_info = leggi_configurazione_info(logger)
    logger.info(
        "[Info] %s",
        "configurata (testo/immagine/audio presenti)" if _info_configurata() else "vuota (link 'Info' nascosto)",
    )

    porta = leggi_porta_server(logger)
    GestoreRichieste.logger = logger

    token_admin = leggi_token_admin(logger)
    GestoreRichieste.token_admin = token_admin
    if token_admin:
        logger.info("Protezione /risultati e /reset attiva (password configurata)")
    else:
        logger.warning("Protezione /risultati e /reset DISATTIVATA (Token_Amministratore vuoto nell'ini)")

    global modalita_coda_attiva, tetto_coda_attivo
    modalita_coda_attiva, tetto_coda_attivo = leggi_modalita_coda(logger)
    logger.info(
        "[Coda] Modalita' coda: '%s'%s",
        modalita_coda_attiva, f" (tetto {tetto_coda_attivo})" if modalita_coda_attiva == "tetto" else "",
    )
    tipo_raffreddamento, valore_raffreddamento = leggi_raffreddamento(logger)
    logger.info("[Raffreddamento] tipo='%s' valore=%d", tipo_raffreddamento, valore_raffreddamento)

    abilitato_lor, porta_api_lor, mappa_nomi_lor = leggi_configurazione_lor(logger)
    if abilitato_lor:
        threading.Thread(
            target=ponte_lor,
            args=(
                logger, porta_api_lor, mappa_nomi_lor, modalita_coda_attiva, tetto_coda_attivo,
                tipo_raffreddamento, valore_raffreddamento,
            ),
            daemon=True,
        ).start()
    else:
        logger.info("[LOR] Integrazione con LOR disabilitata (Abilita_Controllo_LOR = False)")

    server = ServerVoto(("0.0.0.0", porta), GestoreRichieste)
    server.logger = logger
    logger.info(
        "Server di voto in ascolto su http://0.0.0.0:%d (benvenuto: /, voto: /vota, risultati: /risultati)",
        porta,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Interruzione richiesta dall'utente")
    finally:
        server.server_close()
        logger.info("=== VotoShow terminato ===")


if __name__ == "__main__":
    avvia_server()
