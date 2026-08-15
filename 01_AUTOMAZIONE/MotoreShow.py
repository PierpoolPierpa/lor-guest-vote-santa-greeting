"""
MotoreShow.py
Sentinella di monitoraggio per il Christmas Show 2026.

Ruolo: SOLO controllo e log. Legge CONFIGURAZIONE_SHOW.ini ed esegue i
controlli di pre-volo (rete, ping al Portatile se abilitato, presenza
delle cartelle). Ogni problema viene scritto nel log come WARNING o
ERROR, ma non blocca mai l'esecuzione: lo show LOR resta totalmente
indipendente da questo script, che non avvia e non impedisce nulla.
"""

import configparser
import logging
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "CONFIGURAZIONE_SHOW.ini"
CARTELLA_LOG_DI_RISERVA = SCRIPT_DIR / "LOG"


class ConfigurazioneShow:
    """Contiene i parametri letti da CONFIGURAZIONE_SHOW.ini. I valori
    mancanti o non validi vengono sostituiti con un fallback sicuro:
    il caricamento non fallisce mai per un singolo campo malformato."""

    def __init__(self, ip_master, ip_portatile, timeout_ping, tentativi_ping,
                 cartella_sequenze, cartella_log, orario_accensione,
                 orario_spegnimento, giorni_attivi, verifica_portatile):
        self.ip_master = ip_master
        self.ip_portatile = ip_portatile
        self.timeout_ping = timeout_ping
        self.tentativi_ping = tentativi_ping
        self.cartella_sequenze = cartella_sequenze
        self.cartella_log = cartella_log
        self.orario_accensione = orario_accensione
        self.orario_spegnimento = orario_spegnimento
        self.giorni_attivi = giorni_attivi
        self.verifica_portatile = verifica_portatile


def configura_logging(cartella_log: Path) -> logging.Logger:
    """Prepara il logger su file + console. Se la cartella richiesta non
    puo' essere creata, ripiega su CARTELLA_LOG_DI_RISERVA cosi' la
    sentinella riesce comunque a scrivere il proprio log."""
    try:
        cartella_log.mkdir(parents=True, exist_ok=True)
    except OSError:
        cartella_log = CARTELLA_LOG_DI_RISERVA
        cartella_log.mkdir(parents=True, exist_ok=True)

    nome_file = f"MotoreShow_{datetime.now():%Y%m%d}.log"
    percorso_log = cartella_log / nome_file

    logger = logging.getLogger("MotoreShow")
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


def carica_configurazione(percorso_ini: Path, logger: logging.Logger) -> ConfigurazioneShow | None:
    """Legge l'INI e restituisce una ConfigurazioneShow con tutto cio' che
    e' riuscita a leggere. Ogni campo mancante/malformato viene registrato
    come WARNING e sostituito con un fallback: solo se il file proprio non
    esiste o non si riesce a fare il parsing viene restituito None, perche'
    in quel caso non c'e' nulla su cui basare i controlli successivi."""
    if not percorso_ini.is_file():
        logger.error("File di configurazione non trovato: %s", percorso_ini)
        return None

    parser = configparser.ConfigParser()
    try:
        letti = parser.read(percorso_ini, encoding="utf-8")
    except configparser.Error as errore:
        logger.error("Errore nel formato del file INI '%s': %s", percorso_ini, errore)
        return None

    if not letti:
        logger.error("Impossibile leggere il file di configurazione: %s", percorso_ini)
        return None

    for sezione in ("RETE", "PERCORSI", "ORARI", "OPZIONI"):
        if sezione not in parser:
            logger.warning("Sezione [%s] mancante nell'INI: verranno usati i valori di default", sezione)
            parser[sezione] = {}

    rete = parser["RETE"]
    ip_master = rete.get("IP_Master", fallback="").strip()
    ip_portatile = rete.get("IP_Portatile", fallback="").strip()
    timeout_ping = rete.getint("Timeout_Ping", fallback=2)
    tentativi_ping = rete.getint("Tentativi_Ping", fallback=3)

    if not ip_master:
        logger.warning("IP_Master non valorizzato in [RETE]")
    if not ip_portatile:
        logger.warning("IP_Portatile non valorizzato in [RETE]")

    percorsi = parser["PERCORSI"]
    cartella_sequenze_raw = percorsi.get("Cartella_Sequenze", fallback="").strip()
    cartella_log_raw = percorsi.get("Cartella_Log", fallback="LOG").strip()
    if not cartella_sequenze_raw:
        logger.warning("Cartella_Sequenze non valorizzata in [PERCORSI]")

    cartella_sequenze = (SCRIPT_DIR / cartella_sequenze_raw).resolve() if cartella_sequenze_raw else None
    cartella_log = (SCRIPT_DIR / cartella_log_raw).resolve()

    orari = parser["ORARI"]
    orario_accensione = orari.get("Orario_Accensione", fallback="").strip()
    orario_spegnimento = orari.get("Orario_Spegnimento", fallback="").strip()
    giorni_attivi_raw = orari.get("Giorni_Attivi", fallback="0,1,2,3,4,5,6").strip()

    for etichetta, valore in (("Orario_Accensione", orario_accensione),
                               ("Orario_Spegnimento", orario_spegnimento)):
        if valore:
            try:
                datetime.strptime(valore, "%H:%M")
            except ValueError:
                logger.warning("%s non e' un orario valido (HH:MM): '%s'", etichetta, valore)

    try:
        giorni_attivi = [int(g.strip()) for g in giorni_attivi_raw.split(",") if g.strip()]
    except ValueError:
        logger.warning("Giorni_Attivi contiene valori non numerici: '%s'", giorni_attivi_raw)
        giorni_attivi = []

    opzioni = parser["OPZIONI"]
    verifica_portatile = opzioni.getboolean("Verifica_Portatile", fallback=True)

    logger.info("Configurazione caricata da %s", percorso_ini)
    return ConfigurazioneShow(
        ip_master=ip_master,
        ip_portatile=ip_portatile,
        timeout_ping=timeout_ping,
        tentativi_ping=tentativi_ping,
        cartella_sequenze=cartella_sequenze,
        cartella_log=cartella_log,
        orario_accensione=orario_accensione,
        orario_spegnimento=orario_spegnimento,
        giorni_attivi=giorni_attivi,
        verifica_portatile=verifica_portatile,
    )


def verifica_cartella(percorso: Path | None, etichetta: str, logger: logging.Logger) -> None:
    """Controlla che una cartella esista e logga il risultato. Solo
    monitoraggio: non crea la cartella e non interrompe nulla."""
    if percorso is None:
        logger.warning("[CARTELLE] %s non configurata: controllo saltato", etichetta)
        return
    if percorso.is_dir():
        logger.info("[CARTELLE] %s OK: %s", etichetta, percorso)
    else:
        logger.error("[CARTELLE] %s NON TROVATA: %s", etichetta, percorso)


def verifica_raggiungibilita(ip: str, timeout_secondi: int, tentativi: int,
                              logger: logging.Logger) -> bool:
    """Esegue un ping verso l'IP indicato usando il comando di sistema.
    Ritorna True se almeno un tentativo ha successo. Logga sempre
    l'esito ma non solleva mai eccezioni verso il chiamante."""
    is_windows = platform.system().lower() == "windows"
    flag_conteggio = "-n" if is_windows else "-c"
    flag_timeout = "-w" if is_windows else "-W"
    timeout_ms_o_sec = str(timeout_secondi * 1000) if is_windows else str(timeout_secondi)

    for tentativo in range(1, tentativi + 1):
        comando = ["ping", flag_conteggio, "1", flag_timeout, timeout_ms_o_sec, ip]
        try:
            risultato = subprocess.run(
                comando,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_secondi + 2,
            )
            if risultato.returncode == 0:
                logger.info("[RETE] %s raggiungibile (tentativo %d/%d)", ip, tentativo, tentativi)
                return True
            logger.warning("[RETE] Ping a %s fallito (tentativo %d/%d)", ip, tentativo, tentativi)
        except subprocess.TimeoutExpired:
            logger.warning("[RETE] Timeout ping verso %s (tentativo %d/%d)", ip, tentativo, tentativi)
        except OSError as errore:
            logger.error("[RETE] Errore eseguendo ping verso %s: %s", ip, errore)
            return False

    logger.error("[RETE] %s NON raggiungibile dopo %d tentativi", ip, tentativi)
    return False


def esegui_controlli_prevolo(config: ConfigurazioneShow, logger: logging.Logger) -> None:
    """Esegue tutti i controlli di monitoraggio (cartelle, rete). Ogni
    esito viene solo loggato: la sentinella non decide mai se lo show
    puo' partire o no, quello resta compito esclusivo di LOR."""
    logger.info("IP Master: %s | IP Portatile: %s", config.ip_master or "N/D", config.ip_portatile or "N/D")
    logger.info("Orari show: %s - %s | Giorni attivi: %s",
                config.orario_accensione or "N/D", config.orario_spegnimento or "N/D",
                config.giorni_attivi)

    verifica_cartella(config.cartella_sequenze, "Cartella_Sequenze", logger)
    verifica_cartella(config.cartella_log, "Cartella_Log", logger)

    if not config.verifica_portatile:
        logger.info("[RETE] Verifica Portatile disabilitata (Verifica_Portatile = False): ping saltato")
    elif not config.ip_portatile:
        logger.warning("[RETE] Verifica Portatile abilitata ma IP_Portatile non configurato: ping saltato")
    else:
        verifica_raggiungibilita(
            config.ip_portatile, config.timeout_ping, config.tentativi_ping, logger
        )


def inizializza() -> None:
    """Punto di ingresso della sentinella: prepara il log, carica la
    configurazione ed esegue i controlli di pre-volo. Termina sempre
    segnalando lo stato PRONTO, indipendentemente dagli esiti dei
    controlli, perche' non deve mai interferire con l'avvio dello show."""
    logger = configura_logging(CARTELLA_LOG_DI_RISERVA)
    logger.info("=== Avvio Sentinella MotoreShow ===")

    config = carica_configurazione(CONFIG_PATH, logger)

    if config is None:
        logger.error("Configurazione non disponibile: nessun controllo puo' essere eseguito")
        logger.info("=== Stato Sentinella: PRONTO (nessun dato da monitorare) ===")
        return

    if config.cartella_log != CARTELLA_LOG_DI_RISERVA:
        logger = configura_logging(config.cartella_log)
        logger.info("Logging ricollegato alla cartella configurata: %s", config.cartella_log)

    esegui_controlli_prevolo(config, logger)

    logger.info("=== Stato Sentinella: PRONTO (vedi sopra eventuali WARNING/ERROR) ===")


if __name__ == "__main__":
    inizializza()
    sys.exit(0)
