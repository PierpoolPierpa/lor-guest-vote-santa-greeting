"""
PuliziaPreShow.py
Pulizia automatica del PC qualche minuto prima dell'avvio dello show
(configurabile in [PULIZIA] di CONFIGURAZIONE_SHOW.ini):
  - chiude Chrome forzatamente (libera la RAM che tiene occupata)
  - svuota i file temporanei di Windows e SOLO la cache di Chrome
    (mai Cronologia/Password/Preferiti/Preferenze: quelli restano intatti)

Pensato per essere lanciato dall'Utilita' di Pianificazione di Windows
qualche minuto prima di Orario_Accensione, ma si puo' anche lanciare a
mano (pulsante "Pulisci ora" in ManagerShow.pyw).

Ogni passo e' avvolto in try/except: un file bloccato o un errore su una
singola cartella non interrompe il resto della pulizia.
"""

import configparser
import os
import shutil
import subprocess
import tkinter as tk
from datetime import date, datetime
from pathlib import Path

SECONDI_TIMEOUT_CONFERMA = 20

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "CONFIGURAZIONE_SHOW.ini"
CARTELLA_LOG = SCRIPT_DIR / "LOG"
FILE_LOG_PULIZIA = CARTELLA_LOG / "PuliziaPreShow.log"

CREATE_NO_WINDOW = 0x08000000

# Sottocartelle del profilo Chrome che sono davvero solo cache (mai
# Bookmarks, History, Login Data, Preferences: quelle non si toccano)
SOTTOCARTELLE_CACHE_CHROME = ("Cache", "Code Cache", "GPUCache")


def _log(messaggio: str) -> None:
    CARTELLA_LOG.mkdir(parents=True, exist_ok=True)
    riga = f"{datetime.now():%Y-%m-%d %H:%M:%S} {messaggio}\n"
    with open(FILE_LOG_PULIZIA, "a", encoding="utf-8") as f:
        f.write(riga)


def leggi_configurazione_pulizia() -> dict:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    if "PULIZIA" not in parser:
        return {
            "abilita": False, "chiudi_chrome": False, "pulisci_temp": False,
            "programmi_extra": [], "blocca_aggiornamenti": False,
        }
    sezione = parser["PULIZIA"]
    grezzo_programmi = sezione.get("Programmi_Extra", fallback="").strip()
    programmi_extra = [p.strip() for p in grezzo_programmi.split(",") if p.strip()]
    return {
        "abilita": sezione.getboolean("Abilita_Pulizia", fallback=True),
        "chiudi_chrome": sezione.getboolean("Chiudi_Chrome", fallback=True),
        "pulisci_temp": sezione.getboolean("Pulisci_File_Temporanei", fallback=True),
        "programmi_extra": programmi_extra,
        "blocca_aggiornamenti": sezione.getboolean("Blocca_Aggiornamenti_Windows", fallback=False),
    }


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


def conferma_chiusura(nome_processo: str, secondi_timeout: int = SECONDI_TIMEOUT_CONFERMA) -> bool:
    """Chiede conferma prima di chiudere un programma (Chrome o uno dei
    'programmi extra'): se qualcuno lo sta usando in quel momento, non lo
    chiude senza chiedere. Ha un conto alla rovescia con timeout: se
    nessuno risponde (es. pulizia notturna senza nessuno davanti al PC),
    di default NON lo chiude - non blocca la pulizia in eterno."""
    risposta = {"valore": False}
    radice = tk.Tk()
    radice.title("Conferma chiusura")
    radice.attributes("-topmost", True)
    radice.resizable(False, False)

    var_testo = tk.StringVar()

    def _si():
        risposta["valore"] = True
        radice.destroy()

    def _no():
        risposta["valore"] = False
        radice.destroy()

    def _aggiorna_conto(rimasti):
        var_testo.set(
            f"Sto per chiudere {nome_processo}.\nContinuare?\n\n"
            f"(se non rispondi entro {rimasti}s resta aperto)"
        )
        if rimasti > 0:
            radice.after(1000, _aggiorna_conto, rimasti - 1)
        else:
            _no()

    tk.Label(radice, textvariable=var_testo, padx=24, pady=16, justify="center").pack()
    frame_bottoni = tk.Frame(radice)
    frame_bottoni.pack(pady=(0, 16))
    tk.Button(frame_bottoni, text="Si', chiudi", command=_si, width=12).pack(side="left", padx=8)
    tk.Button(frame_bottoni, text="No, lascia aperto", command=_no, width=16).pack(side="left", padx=8)

    radice.protocol("WM_DELETE_WINDOW", _no)
    _aggiorna_conto(secondi_timeout)
    radice.mainloop()
    return risposta["valore"]


def chiudi_processo(nome_eseguibile: str) -> None:
    risultato = subprocess.run(
        ["taskkill", "/IM", nome_eseguibile, "/F"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    if risultato.returncode == 0:
        _log(f"{nome_eseguibile} chiuso.")
    else:
        _log(f"{nome_eseguibile} non era aperto (o non e' stato possibile chiuderlo): nessuna azione necessaria.")


def chiudi_chrome() -> None:
    chiudi_processo("chrome.exe")


def blocca_aggiornamenti_windows() -> None:
    """Tenta di fermare i servizi di Windows Update (wuauserv, bits) cosi'
    non scattano riavvii/installazioni durante lo show. Richiede diritti
    di amministratore: se questo script gira come utente normale (caso
    piu' comune per un task pianificato creato da ManagerShow.pyw), il
    tentativo fallisce e va segnalato chiaramente nel log - la pulizia
    del resto continua comunque, non e' un errore bloccante."""
    riuscito = True
    for servizio in ("wuauserv", "bits"):
        risultato = subprocess.run(
            ["net", "stop", servizio],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        if risultato.returncode != 0:
            riuscito = False

    if riuscito:
        _log("Windows Update bloccato automaticamente (servizi wuauserv/bits fermati).")
    else:
        _log(
            "ATTENZIONE: non sono riuscito a bloccare Windows Update in automatico "
            "(serve eseguire il task pianificato con diritti di amministratore). "
            "Blocca gli aggiornamenti A MANO, almeno 2-3 ore prima dello show."
        )


def _svuota_cartella(cartella: Path) -> tuple:
    """Cancella il CONTENUTO di una cartella (non la cartella stessa).
    Ritorna (elementi_cancellati, elementi_saltati)."""
    cancellati = 0
    saltati = 0
    if not cartella.is_dir():
        return cancellati, saltati

    for elemento in cartella.iterdir():
        try:
            if elemento.is_dir():
                shutil.rmtree(elemento)
            else:
                elemento.unlink()
            cancellati += 1
        except OSError:
            saltati += 1  # file in uso, permessi, ecc: lo saltiamo e andiamo avanti

    return cancellati, saltati


def pulisci_file_temporanei() -> None:
    cartella_temp = Path(os.environ.get("TEMP", ""))
    if cartella_temp.is_dir():
        cancellati, saltati = _svuota_cartella(cartella_temp)
        _log(f"Temp di Windows: {cancellati} elementi rimossi, {saltati} saltati (in uso).")
    else:
        _log("Cartella TEMP di Windows non trovata: saltata.")

    cartella_profilo_chrome = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Default"
    if cartella_profilo_chrome.is_dir():
        for nome_sottocartella in SOTTOCARTELLE_CACHE_CHROME:
            cancellati, saltati = _svuota_cartella(cartella_profilo_chrome / nome_sottocartella)
            _log(f"Cache Chrome '{nome_sottocartella}': {cancellati} elementi rimossi, {saltati} saltati.")
    else:
        _log("Profilo Chrome non trovato: cache Chrome saltata.")


def esegui_pulizia() -> None:
    _log("=== PuliziaPreShow: avvio ===")
    configurazione = leggi_configurazione_pulizia()

    if not configurazione["abilita"]:
        _log("Pulizia disabilitata da configurazione (Abilita_Pulizia = False): nessuna azione.")
        return

    if configurazione["chiudi_chrome"]:
        if conferma_chiusura("Chrome"):
            chiudi_chrome()
        else:
            _log("Chiusura di Chrome saltata (nessuna conferma dal popup).")

    for nome_eseguibile in configurazione["programmi_extra"]:
        if conferma_chiusura(nome_eseguibile):
            chiudi_processo(nome_eseguibile)
        else:
            _log(f"Chiusura di {nome_eseguibile} saltata (nessuna conferma dal popup).")

    if configurazione["pulisci_temp"]:
        pulisci_file_temporanei()

    if configurazione["blocca_aggiornamenti"]:
        blocca_aggiornamenti_windows()

    _log("=== PuliziaPreShow: completata ===")


if __name__ == "__main__":
    if periodo_attivo():
        esegui_pulizia()
    else:
        _log("Fuori dal periodo attivo (Data_Inizio/Data_Fine in [ORARI]): nessuna azione.")
