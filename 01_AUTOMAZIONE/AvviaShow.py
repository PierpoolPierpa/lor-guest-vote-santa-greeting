"""
AvviaShow.py
Avvia VotoShow.py e MotoreShow.py in background (nessuna finestra
visibile). Richiamato da ManagerShow.pyw (pulsante "Avvia ora" o
pianificazione tramite l'Utilita' di Pianificazione di Windows).

Salva il PID di ogni processo avviato in un file: FermaShow.py legge
quei PID per sapere ESATTAMENTE quali processi fermare, senza rischiare
di toccare altri processi Python eventualmente aperti sul PC.

Nota tecnica: i processi figli vengono avviati con python.exe (non
pythonw.exe) perche' VotoShow.py e MotoreShow.py scrivono anche su
console (oltre che su file di log): con pythonw.exe sys.stdout e'
None e quel logging andrebbe in crash. La finestra resta comunque
invisibile grazie al flag CREATE_NO_WINDOW.

Estensione .py (non .pyw): serve per poter essere importato da
ManagerShow.pyw. Quando lo lancia l'Utilita' di Pianificazione, la
finestra resta comunque nascosta perche' li' specifichiamo esplicitamente
pythonw.exe come programma da eseguire.
"""

import configparser
import socket
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CARTELLA_FILE_OPERATIVI = SCRIPT_DIR.parent
FILE_VOTOSHOW = CARTELLA_FILE_OPERATIVI / "02_SERVER_VOTO" / "VotoShow.py"
FILE_MOTORESHOW = SCRIPT_DIR / "MotoreShow.py"
CARTELLA_LOG = SCRIPT_DIR / "LOG"
FILE_PID_VOTO = CARTELLA_LOG / "pid_votoshow.txt"
FILE_PID_MOTORE = CARTELLA_LOG / "pid_motoreshow.txt"
FILE_LOG_AVVIO = CARTELLA_LOG / "AvviaShow.log"
CONFIG_PATH = SCRIPT_DIR / "CONFIGURAZIONE_SHOW.ini"


def _porta_votoshow() -> int:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    return parser.getint("VOTO", "Porta_Server", fallback=8080)


def periodo_attivo() -> bool:
    """Vero se oggi cade dentro [Data_Inizio, Data_Fine] di [ORARI] in
    CONFIGURAZIONE_SHOW.ini (formato AAAA-MM-GG, estremi inclusi). Se uno
    dei due campi e' vuoto, quel lato resta senza limite. Se sono
    entrambi vuoti (default), sempre attivo - stesso comportamento di
    prima dell'introduzione del periodo. Una data mal formattata viene
    ignorata (non blocchiamo lo show per un errore di battitura)."""
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

PYTHON_EXE = Path(sys.executable).parent / "python.exe"
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _log(messaggio: str) -> None:
    CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
    riga = f"{datetime.now():%Y-%m-%d %H:%M:%S} {messaggio}\n"
    with open(FILE_LOG_AVVIO, "a", encoding="utf-8") as f:
        f.write(riga)


def verifica_in_esecuzione(file_pid: Path) -> bool:
    """Vero se il PID salvato in file_pid corrisponde a un processo
    python.exe ancora attivo. Usata anche da ManagerShow.pyw per
    mostrare lo stato acceso/spento nella GUI."""
    if not file_pid.is_file():
        return False
    try:
        pid = int(file_pid.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    risultato = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return "python" in risultato.stdout.lower()


def _porta_libera(porta: int) -> bool:
    """Vero se nessun processo sta ascoltando su 127.0.0.1:porta in questo
    momento. A differenza del PID file (che sa solo cosa abbiamo avviato
    NOI), questo controlla lo stato reale della porta: l'unico modo per
    accorgersi di un processo orfano che nessuno sta piu' tracciando."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", porta))
        except OSError:
            return True
        return False


def _trova_pid_su_porta(porta: int) -> int | None:
    """Cerca con netstat quale PID ha la porta in LISTENING. None se non
    lo trova (es. e' un programma non-Windows-standard o la porta si e'
    liberata nel frattempo)."""
    risultato = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    for riga in risultato.stdout.splitlines():
        parti = riga.split()
        if len(parti) >= 5 and parti[0] == "TCP" and parti[-2] == "LISTENING" and parti[-1].isdigit():
            indirizzo_locale = parti[1]
            if indirizzo_locale.endswith(f":{porta}"):
                return int(parti[-1])
    return None


def avvia(percorso_script: Path, file_pid: Path, priorita_bassa: bool = False, porta: int | None = None) -> bool:
    """priorita_bassa=True fa partire il processo con priorita' Windows
    "Below Normal": in caso di sovraccarico (es. VotoShow sotto un flood
    di richieste), lo scheduler di Windows favorisce sempre gli altri
    processi sullo stesso PC - in particolare MotoreShow, che pilota le
    luci e non deve mai perdere i tempi per colpa del server di voto.

    porta: se indicata (solo per VotoShow.py, che e' l'unico ad aprire
    una porta HTTP), prima di avviare verifica che sia DAVVERO libera.
    ThreadingHTTPServer usa allow_reuse_address, quindi su Windows un
    processo orfano rimasto attaccato alla porta non impedisce a un
    nuovo processo di avviarsi e loggare correttamente: le richieste
    pero' possono continuare ad arrivare al vecchio processo invece che
    al nuovo. Qui rileviamo l'orfano ed eliminiamo il rischio alla
    radice invece di sperare in un timing giusto tra stop e avvio."""
    if not percorso_script.is_file():
        _log(f"ERRORE: script non trovato: {percorso_script}")
        return False
    if verifica_in_esecuzione(file_pid):
        _log(f"Gia' in esecuzione, salto: {percorso_script.name}")
        return True

    if porta is not None and not _porta_libera(porta):
        pid_orfano = _trova_pid_su_porta(porta)
        if pid_orfano is not None:
            _log(
                f"ATTENZIONE: porta {porta} occupata da un processo non tracciato "
                f"(PID {pid_orfano}). Lo termino prima di avviare {percorso_script.name}."
            )
            subprocess.run(
                ["taskkill", "/PID", str(pid_orfano), "/F"],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        for _ in range(20):
            if _porta_libera(porta):
                break
            time.sleep(0.25)
        if not _porta_libera(porta):
            _log(f"ERRORE: porta {porta} ancora occupata dopo il tentativo di liberarla, non avvio {percorso_script.name}.")
            return False

    flag_creazione = CREATE_NO_WINDOW
    if priorita_bassa:
        flag_creazione |= BELOW_NORMAL_PRIORITY_CLASS

    processo = subprocess.Popen(
        [str(PYTHON_EXE), str(percorso_script)],
        cwd=str(percorso_script.parent),
        creationflags=flag_creazione,
    )
    CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
    file_pid.write_text(str(processo.pid), encoding="utf-8")
    _log(f"Avviato {percorso_script.name} (PID {processo.pid})" + (" [priorita' bassa]" if priorita_bassa else ""))
    return True


if __name__ == "__main__":
    _log("=== AvviaShow: avvio richiesto ===")
    if periodo_attivo():
        avvia(FILE_VOTOSHOW, FILE_PID_VOTO, priorita_bassa=True, porta=_porta_votoshow())
        avvia(FILE_MOTORESHOW, FILE_PID_MOTORE)
    else:
        _log("Fuori dal periodo attivo (Data_Inizio/Data_Fine in [ORARI]): nessuna azione.")
