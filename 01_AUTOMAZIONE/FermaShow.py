"""
FermaShow.py
Ferma i processi avviati da AvviaShow.py, leggendo i PID salvati in
LOG\\pid_votoshow.txt e LOG\\pid_motoreshow.txt. Richiamato da
ManagerShow.pyw (pulsante "Ferma ora" o pianificazione tramite
l'Utilita' di Pianificazione di Windows).

Prima di terminare un PID controlla che sia davvero un processo
python.exe (con tasklist): se quel PID e' gia' terminato da solo o e'
stato riassegnato nel frattempo a un altro programma, non lo tocca.
"""

import configparser
import socket
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "CONFIGURAZIONE_SHOW.ini"
CARTELLA_LOG = SCRIPT_DIR / "LOG"
FILE_PID_VOTO = CARTELLA_LOG / "pid_votoshow.txt"
FILE_PID_MOTORE = CARTELLA_LOG / "pid_motoreshow.txt"
FILE_LOG_STOP = CARTELLA_LOG / "FermaShow.log"

CREATE_NO_WINDOW = 0x08000000


def _log(messaggio: str) -> None:
    CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
    riga = f"{datetime.now():%Y-%m-%d %H:%M:%S} {messaggio}\n"
    with open(FILE_LOG_STOP, "a", encoding="utf-8") as f:
        f.write(riga)


def periodo_attivo() -> bool:
    """Stessa logica di AvviaShow.periodo_attivo() (vedi li' per i
    dettagli): oggi deve cadere dentro [Data_Inizio, Data_Fine] di
    [ORARI], oppure quei campi devono essere vuoti."""
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    grezzo_inizio = parser.get("ORARI", "Data_Inizio", fallback="").strip()
    grezzo_fine = parser.get("ORARI", "Data_Fine", fallback="").strip()
    oggi = date.today()
    try:
        if grezzo_inizio and oggi < date.fromisoformat(grezzo_inizio):
            return False
        if grezzo_fine and oggi > date.fromisoformat(grezzo_fine):
            return False
    except ValueError:
        return True
    return True


def _e_processo_python(pid: int) -> bool:
    risultato = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return "python" in risultato.stdout.lower()


def _porta_libera(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", porta))
        except OSError:
            return True
        return False


def ferma(file_pid: Path, porta: int | None = None) -> None:
    """porta: se indicata (solo per VotoShow.py), dopo il taskkill
    attende che la porta sia DAVVERO libera invece di fidarsi solo del
    fatto che il PID tracciato sia sparito da tasklist - un riavvio
    lanciato subito dopo, prima che Windows abbia rilasciato la porta,
    e' proprio cio' che permetteva a un processo orfano di restare
    attaccato alla porta insieme al nuovo (vedi commento in
    AvviaShow.avvia)."""
    if not file_pid.is_file():
        return

    try:
        pid = int(file_pid.read_text(encoding="utf-8").strip())
    except ValueError:
        file_pid.unlink(missing_ok=True)
        return

    if _e_processo_python(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        _log(f"Terminato PID {pid} ({file_pid.stem})")
    else:
        _log(f"PID {pid} ({file_pid.stem}) non e' piu' un processo python: nessuna azione")

    file_pid.unlink(missing_ok=True)

    if porta is not None:
        for _ in range(20):
            if _porta_libera(porta):
                break
            time.sleep(0.25)
        else:
            _log(f"ATTENZIONE: porta {porta} ancora occupata 5 secondi dopo l'arresto di {file_pid.stem}.")


def riattiva_aggiornamenti_windows() -> None:
    """Controparte di blocca_aggiornamenti_windows() in PuliziaPreShow.py:
    a show finito riavvia i servizi di Windows Update, se erano stati
    bloccati automaticamente (best-effort, richiede diritti admin come
    il blocco: se fallisce lo si segnala solo nel log, non blocca nulla)."""
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    if not parser.has_section("PULIZIA") or not parser.getboolean(
        "PULIZIA", "Blocca_Aggiornamenti_Windows", fallback=False
    ):
        return

    riuscito = True
    for servizio in ("wuauserv", "bits"):
        risultato = subprocess.run(
            ["net", "start", servizio],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        if risultato.returncode != 0:
            riuscito = False

    if riuscito:
        _log("Windows Update riattivato (servizi wuauserv/bits riavviati).")
    else:
        _log("Non sono riuscito a riavviare Windows Update in automatico (probabile mancanza di diritti admin).")


if __name__ == "__main__":
    _log("=== FermaShow: arresto richiesto ===")
    if periodo_attivo():
        ferma(FILE_PID_VOTO)
        ferma(FILE_PID_MOTORE)
        riattiva_aggiornamenti_windows()
    else:
        _log("Fuori dal periodo attivo (Data_Inizio/Data_Fine in [ORARI]): nessuna azione.")
