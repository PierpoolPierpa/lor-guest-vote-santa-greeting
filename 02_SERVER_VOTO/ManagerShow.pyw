"""
ManagerShow.pyw
GUI unificata per gestire lo show: canzoni votabili (aggiungi/modifica/
escludi/rinomina) + programmazione avvio/arresto automatico di
VotoShow.py e MotoreShow.py, con indicatore di stato acceso/spento.

Sostituisce i vecchi GestisciCanzoni.pyw e PianificaShow.pyw (ora unificati
qui in due schede della stessa finestra).

IMPORTANTE: questa GUI non cancella MAI fisicamente file .loredit/audio
dal disco. "Escludere" una canzone significa solo toglierla dalla lista
votabile: le sequenze restano intatte per LOR e si possono reincludere
in qualsiasi momento.

Dopo ogni modifica alle canzoni viene rigenerato catalogo_canzoni.json.
VotoShow.py legge quel file solo all'avvio: se e' gia' in esecuzione
durante lo show, va riavviato perche' le modifiche abbiano effetto.
"""

import configparser
import csv
import ctypes
import ctypes.wintypes
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from traduzioni import LINGUA_DEFAULT, testo

# matplotlib e' opzionale: serve solo per il grafico nella scheda Storico,
# il resto della GUI funziona anche senza (vedi _installa_matplotlib per
# l'installazione automatica su richiesta, stesso schema gia' usato per
# 'mutagen' e 'Pillow').
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    MATPLOTLIB_DISPONIBILE = True
except ImportError:
    MATPLOTLIB_DISPONIBILE = False

# tkcalendar e' opzionale: serve solo per il pulsante "calendario" delle
# date nella scheda Grafico (senza, le date si scelgono dalla tendina o
# si scrivono a mano, entrambe funzionano comunque).
try:
    from tkcalendar import Calendar as WidgetCalendario
    TKCALENDAR_DISPONIBILE = True
except ImportError:
    TKCALENDAR_DISPONIBILE = False

try:
    import CostruisciCatalogo as cat
except SystemExit:
    # CostruisciCatalogo.py chiama sys.exit() con un messaggio stampato a
    # console se manca 'mutagen': in un .pyw (nessuna console) quel
    # messaggio sarebbe invisibile, quindi lo intercettiamo qui e offriamo
    # di installarlo da soli usando sys.executable, cosi' va per forza
    # nello stesso identico Python che sta eseguendo questa finestra.
    _radice_temporanea = tk.Tk()
    _radice_temporanea.withdraw()
    _vuole_installare = messagebox.askyesno(
        testo(LINGUA_DEFAULT, "gc_dipendenza_mancante_titolo"),
        testo(LINGUA_DEFAULT, "gc_dipendenza_mancante_testo"),
    )
    if _vuole_installare:
        _risultato = subprocess.run(
            [sys.executable, "-m", "pip", "install", "mutagen"],
            capture_output=True, text=True,
        )
        if _risultato.returncode == 0:
            messagebox.showinfo(
                testo(LINGUA_DEFAULT, "gc_installazione_riuscita_titolo"),
                testo(LINGUA_DEFAULT, "gc_installazione_riuscita_testo"),
            )
        else:
            messagebox.showerror(
                testo(LINGUA_DEFAULT, "gc_installazione_fallita_titolo"),
                testo(LINGUA_DEFAULT, "gc_installazione_fallita_testo", errore=_risultato.stderr[-500:]),
            )
    raise SystemExit(1)

CONFIG_PATH = cat.CONFIG_PATH
SEZIONE_TRIGGER = "TRIGGER_LOR"
SEZIONE_SEQUENZE_LOR = "SEQUENZE_LOR"
SEZIONE_VOTO = "VOTO"
SEZIONE_ORARI = "ORARI"
SEZIONE_PULIZIA = "PULIZIA"
SEZIONE_BENVENUTO = "BENVENUTO"
SEZIONE_SALUTO_BABBONATALE = "SALUTO_BABBONATALE"
SEZIONE_INFO = "INFO"
SEZIONE_QRCODE = "QRCODE"
SEZIONE_INTERFACCIA = "INTERFACCIA"
CARTELLA_BENVENUTO = cat.SCRIPT_DIR / "BENVENUTO"
CARTELLA_INFO = cat.SCRIPT_DIR / "INFO"
COLORE_BENVENUTO_DEFAULT = "#1e1e1e"
COLORE_INFO_DEFAULT = "#1e1e1e"

# Archivio profili QR Code (una WiFi+password+QR per riga, es. "rete
# ospiti" e "rete privata"): dati personali (password WiFi in chiaro),
# mai nel repository - vedi .gitignore.
CARTELLA_QR_PROFILI = cat.SCRIPT_DIR / "QR_PROFILI"
FILE_QR_PROFILI = CARTELLA_QR_PROFILI / "profili.json"

# Database storico (stesso file che scrive VotoShow.py): qui lo si legge
# soltanto, per la scheda "Storico". Se VotoShow.py non e' mai stato
# avviato il file non esiste ancora - gestito a parte in _aggiorna_storico.
FILE_DATABASE_STORICO = cat.SCRIPT_DIR / "LOG" / "storico_voti.sqlite3"


def _connetti_storico():
    if not FILE_DATABASE_STORICO.is_file():
        return None
    return sqlite3.connect(FILE_DATABASE_STORICO)


def date_disponibili_storico() -> list:
    connessione = _connetti_storico()
    if connessione is None:
        return []
    try:
        righe = connessione.execute(
            "SELECT DISTINCT data FROM (SELECT data FROM voti UNION SELECT data FROM vincitori) ORDER BY data DESC"
        ).fetchall()
        return [r[0] for r in righe]
    finally:
        connessione.close()


def riepilogo_popolarita_storico(data, includi_test: bool) -> list:
    """data=None -> tutto lo storico, altrimenti filtra sulla data (YYYY-MM-DD).
    Ritorna righe (titolo, voti_totali, volte_mandata_in_onda), ordinate
    dalla piu' votata alla meno votata."""
    connessione = _connetti_storico()
    if connessione is None:
        return []
    try:
        clausola_data = "" if data is None else "AND data = ?"
        parametri = () if data is None else (data,)
        clausola_test = "" if includi_test else "AND test = 0"

        voti_per_titolo = dict(connessione.execute(
            f"SELECT canzone_titolo, COUNT(*) FROM voti WHERE 1=1 {clausola_data} {clausola_test} "
            f"GROUP BY canzone_titolo",
            parametri,
        ).fetchall())
        vinte_per_titolo = dict(connessione.execute(
            f"SELECT canzone_titolo, COUNT(*) FROM vincitori WHERE 1=1 {clausola_data} {clausola_test} "
            f"GROUP BY canzone_titolo",
            parametri,
        ).fetchall())
        titoli = set(voti_per_titolo) | set(vinte_per_titolo)
        righe = [
            (titolo, voti_per_titolo.get(titolo, 0), vinte_per_titolo.get(titolo, 0))
            for titolo in titoli
        ]
        righe.sort(key=lambda r: (-r[1], r[0]))
        return righe
    finally:
        connessione.close()


def cronologia_giorno_storico(data: str, includi_test: bool) -> list:
    """Righe (ora, titolo, voti_ricevuti) per quel giorno, in ordine
    cronologico: ogni volta che una sequenza e' stata mandata in onda."""
    connessione = _connetti_storico()
    if connessione is None:
        return []
    try:
        clausola_test = "" if includi_test else "AND test = 0"
        return connessione.execute(
            f"SELECT ora, canzone_titolo, voti_ricevuti FROM vincitori WHERE data = ? {clausola_test} ORDER BY ora",
            (data,),
        ).fetchall()
    finally:
        connessione.close()


def elimina_dati_giorno_storico(data: str) -> None:
    connessione = _connetti_storico()
    if connessione is None:
        return
    try:
        connessione.execute("DELETE FROM voti WHERE data = ?", (data,))
        connessione.execute("DELETE FROM vincitori WHERE data = ?", (data,))
        connessione.commit()
    finally:
        connessione.close()


def andamento_voti_giornalieri_storico(includi_test: bool, data_da: str = None, data_a: str = None) -> list:
    """Righe (data_iso, voti_totali_del_giorno), un punto per ogni giorno
    con almeno un voto nell'intervallo [data_da, data_a] (estremi inclusi,
    None = senza limite da quel lato), in ordine cronologico - per il
    grafico a linea dell'andamento dello show."""
    connessione = _connetti_storico()
    if connessione is None:
        return []
    try:
        clausola_test = "" if includi_test else "AND test = 0"
        clausola_range = ""
        parametri = []
        if data_da:
            clausola_range += " AND data >= ?"
            parametri.append(data_da)
        if data_a:
            clausola_range += " AND data <= ?"
            parametri.append(data_a)
        return connessione.execute(
            f"SELECT data, COUNT(*) FROM voti WHERE 1=1 {clausola_test} {clausola_range} GROUP BY data ORDER BY data",
            parametri,
        ).fetchall()
    finally:
        connessione.close()

# Test multi-voto: ogni finestra usa un profilo di Chrome/Edge temporaneo e
# vuoto (cookie propri, non condivisi con le altre) cosi' conta come un
# elettore diverso. I profili vivono sotto %TEMP%, in una cartella dedicata
# che viene ripulita ad ogni uso per non accumulare roba sul disco del PC
# dello show.
CARTELLA_PROFILI_TEST_VOTO = Path(tempfile.gettempdir()) / "VotoShow_TestMultiVoto"
PERCORSI_BROWSER_CANDIDATI = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]


def trova_browser_con_profili() -> str:
    """Cerca Chrome o Edge in un percorso standard di Windows: sono gli
    unici due che sappiamo di sicuro supportare '--user-data-dir' per
    aprire una finestra con un profilo isolato (cookie propri). Stringa
    vuota se nessuno dei due e' installato in un percorso noto."""
    for percorso_grezzo in PERCORSI_BROWSER_CANDIDATI:
        percorso = os.path.expandvars(percorso_grezzo)
        if Path(percorso).is_file():
            return percorso
    return ""

# AvviaShow.py/FermaShow.py/PuliziaPreShow.py vivono in 01_AUTOMAZIONE, non
# nella stessa cartella di questo file: li aggiungiamo al percorso di
# ricerca moduli.
CARTELLA_AUTOMAZIONE = CONFIG_PATH.parent
sys.path.insert(0, str(CARTELLA_AUTOMAZIONE))
import AvviaShow  # noqa: E402
import FermaShow  # noqa: E402
import PuliziaPreShow  # noqa: E402

NOME_TASK_AVVIO = "V1965_Show_Avvio"
NOME_TASK_STOP = "V1965_Show_Stop"
NOME_TASK_PULIZIA = "V1965_Show_Pulizia"
GIORNI_SETTIMANA = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
MAPPA_GIORNI_SCHTASKS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def leggi_lingua_iniziale() -> str:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    lingua = parser.get(SEZIONE_VOTO, "Lingua_Interfaccia", fallback=LINGUA_DEFAULT).strip().lower()
    return lingua if lingua in ("it", "en") else LINGUA_DEFAULT


# ----------------------------------------------------------------------
# Lettura/scrittura dirette del CONFIGURAZIONE_SHOW.ini
# ----------------------------------------------------------------------
def carica_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    for sezione in (
        "PERCORSI", SEZIONE_VOTO, SEZIONE_TRIGGER, SEZIONE_SEQUENZE_LOR, SEZIONE_ORARI, SEZIONE_BENVENUTO,
        SEZIONE_SALUTO_BABBONATALE, SEZIONE_INFO, SEZIONE_QRCODE,
    ):
        if sezione not in parser:
            parser[sezione] = {}
    return parser


def salva_parser(parser: configparser.ConfigParser) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        parser.write(f)


def leggi_cartella_sequenze(parser: configparser.ConfigParser) -> Path:
    grezzo = parser.get("PERCORSI", "Cartella_Sequenze", fallback="").strip()
    return (cat.CARTELLA_AUTOMAZIONE / grezzo).resolve()


def leggi_cartelle_escluse(parser: configparser.ConfigParser) -> set:
    grezzo = parser.get(SEZIONE_VOTO, "Cartelle_Escluse", fallback="").strip()
    return {c.strip() for c in grezzo.split(",") if c.strip()}


def scrivi_cartelle_escluse(parser: configparser.ConfigParser, cartelle_escluse: set) -> None:
    parser.set(SEZIONE_VOTO, "Cartelle_Escluse", ", ".join(sorted(cartelle_escluse)))


def leggi_trigger(parser: configparser.ConfigParser, id_canzone: str) -> str:
    return parser.get(SEZIONE_TRIGGER, id_canzone, fallback="")


def scrivi_trigger(parser: configparser.ConfigParser, id_canzone: str, indirizzo: str) -> None:
    if indirizzo.strip():
        parser.set(SEZIONE_TRIGGER, id_canzone, indirizzo.strip())
    elif parser.has_option(SEZIONE_TRIGGER, id_canzone):
        parser.remove_option(SEZIONE_TRIGGER, id_canzone)


def leggi_nome_lor(parser: configparser.ConfigParser, id_canzone: str) -> str:
    return parser.get(SEZIONE_SEQUENZE_LOR, id_canzone, fallback="")


def scrivi_nome_lor(parser: configparser.ConfigParser, id_canzone: str, nome_lor: str) -> None:
    if nome_lor.strip():
        parser.set(SEZIONE_SEQUENZE_LOR, id_canzone, nome_lor.strip())
    elif parser.has_option(SEZIONE_SEQUENZE_LOR, id_canzone):
        parser.remove_option(SEZIONE_SEQUENZE_LOR, id_canzone)


def leggi_config_benvenuto(parser: configparser.ConfigParser) -> dict:
    grezzo = parser.get(SEZIONE_BENVENUTO, "Testo", fallback="")
    return {
        "testo": grezzo.replace("\\n", "\n"),
        "immagine": parser.get(SEZIONE_BENVENUTO, "File_Immagine", fallback="").strip(),
        "audio": parser.get(SEZIONE_BENVENUTO, "File_Audio", fallback="").strip(),
        "colore": parser.get(SEZIONE_BENVENUTO, "Colore_Sfondo", fallback=COLORE_BENVENUTO_DEFAULT).strip() or COLORE_BENVENUTO_DEFAULT,
    }


def scrivi_config_benvenuto(parser: configparser.ConfigParser, testo: str, immagine: str, audio: str, colore: str) -> None:
    # Il file ini scrive ogni valore su una sola riga e usa '%' per
    # l'interpolazione: un testo libero multi-riga (o con un '%' dentro)
    # spaccherebbe il file se scritto cosi' com'e'. '\n' letterale al posto
    # degli a-capo e '%%' al posto di '%' sono le due uniche cose da
    # proteggere; leggi_config_benvenuto() fa l'operazione inversa (il
    # '%%' -> '%' lo fa gia' da solo configparser in lettura).
    testo_sicuro = testo.replace("%", "%%").replace("\r\n", "\n").replace("\n", "\\n")
    parser.set(SEZIONE_BENVENUTO, "Testo", testo_sicuro)
    parser.set(SEZIONE_BENVENUTO, "File_Immagine", immagine)
    parser.set(SEZIONE_BENVENUTO, "File_Audio", audio)
    parser.set(SEZIONE_BENVENUTO, "Colore_Sfondo", colore)


def leggi_config_info(parser: configparser.ConfigParser) -> dict:
    grezzo = parser.get(SEZIONE_INFO, "Testo", fallback="")
    grazie_grezzo = parser.get(SEZIONE_INFO, "Testo_Grazie", fallback="")
    return {
        "testo": grezzo.replace("\\n", "\n"),
        "immagine": parser.get(SEZIONE_INFO, "File_Immagine", fallback="").strip(),
        "audio": parser.get(SEZIONE_INFO, "File_Audio", fallback="").strip(),
        "colore": parser.get(SEZIONE_INFO, "Colore_Sfondo", fallback=COLORE_INFO_DEFAULT).strip() or COLORE_INFO_DEFAULT,
        "visibile": parser.getboolean(SEZIONE_INFO, "Visibile", fallback=False),
        "link_donazione": parser.get(SEZIONE_INFO, "Link_Donazione", fallback="").strip(),
        "testo_grazie": grazie_grezzo.replace("\\n", "\n"),
    }


def scrivi_config_info(
    parser: configparser.ConfigParser, testo: str, immagine: str, audio: str, colore: str,
    visibile: bool, link_donazione: str, testo_grazie: str,
) -> None:
    testo_sicuro = testo.replace("%", "%%").replace("\r\n", "\n").replace("\n", "\\n")
    testo_grazie_sicuro = testo_grazie.replace("%", "%%").replace("\r\n", "\n").replace("\n", "\\n")
    parser.set(SEZIONE_INFO, "Testo", testo_sicuro)
    parser.set(SEZIONE_INFO, "File_Immagine", immagine)
    parser.set(SEZIONE_INFO, "File_Audio", audio)
    parser.set(SEZIONE_INFO, "Colore_Sfondo", colore)
    parser.set(SEZIONE_INFO, "Visibile", "True" if visibile else "False")
    parser.set(SEZIONE_INFO, "Link_Donazione", link_donazione)
    parser.set(SEZIONE_INFO, "Testo_Grazie", testo_grazie_sicuro)


def leggi_sequenze_da_lor(parser: configparser.ConfigParser) -> list:
    """Interroga in diretta l'API di LOR (GET /v1/player/mainSequences) e
    ritorna i nomi esatti delle sequenze nel Main, nell'ordine in cui LOR
    le restituisce (che rispecchia il loro ordine nello show). Solleva
    RuntimeError con un messaggio leggibile se LOR non e' raggiungibile."""
    porta_api = parser.getint(SEZIONE_TRIGGER, "Porta_API_LOR", fallback=8001)
    url = f"http://127.0.0.1:{porta_api}/v1/player/mainSequences"
    try:
        with urllib.request.urlopen(url, timeout=4) as risposta:
            dati = json.loads(risposta.read())
    except (urllib.error.URLError, TimeoutError, OSError) as errore:
        raise RuntimeError(str(errore)) from errore

    voci = dati if isinstance(dati, list) else (dati or {}).get("value", [])
    nomi = [voce.get("name", "").strip() for voce in voci if voce.get("name", "").strip()]
    return nomi


def rigenera_catalogo_sicuro(lingua: str) -> tuple:
    """Richiama CostruisciCatalogo.costruisci_catalogo() proteggendosi dal
    fatto che le sue funzioni interne possono chiamare sys.exit() in caso
    di errore grave (comportamento pensato per l'uso da riga di comando,
    qui invece va trasformato in un messaggio senza chiudere la GUI)."""
    try:
        cat.costruisci_catalogo()
        return True, None
    except SystemExit:
        return False, testo(lingua, "gc_errore_config_generico")
    except Exception as errore:
        return False, str(errore)


# ----------------------------------------------------------------------
# Anteprima di una cartella-sequenza (solo lettura, nessuna copia file:
# quella la fa solo rigenera_catalogo_sicuro())
# ----------------------------------------------------------------------
def anteprima_sequenza(cartella: Path) -> dict:
    id_canzone = cat.crea_id(cartella.name)
    percorso_audio = cat.trova_primo_file(cartella, cat.ESTENSIONI_AUDIO)

    if percorso_audio is None:
        return {
            "id": id_canzone, "cartella": cartella, "titolo": cartella.name,
            "artista": "", "durata_testo": "--:--", "ha_audio": False,
            "percorso_audio": None, "percorso_copertina": None,
        }

    titolo, artista, durata = cat.leggi_metadati_audio(percorso_audio, cartella.name)
    percorso_copertina = cat.trova_primo_file(cartella, cat.ESTENSIONI_IMMAGINE)
    return {
        "id": id_canzone, "cartella": cartella, "titolo": titolo, "artista": artista,
        "durata_testo": cat.formatta_durata(durata), "ha_audio": True,
        "percorso_audio": percorso_audio, "percorso_copertina": percorso_copertina,
    }


# ----------------------------------------------------------------------
# Utilita' di Pianificazione di Windows (schtasks)
# ----------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000


def _task_esiste(nome: str) -> bool:
    risultato = subprocess.run(
        ["schtasks", "/query", "/tn", nome],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return risultato.returncode == 0


def _processo_e_elevato() -> bool:
    """Vero se ManagerShow.pyw sta girando ADESSO con privilegi elevati
    (avviato con 'Esegui come amministratore'). Windows nega la creazione
    di un task /RL HIGHEST se il processo che lo crea non e' gia' lui
    stesso elevato in questo momento - non basta che l'account sia
    amministratore, serve che sia elevato ora."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _crea_task(nome: str, percorso_script: Path, giorni_schtasks: list, orario: str) -> tuple:
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    comando = [
        "schtasks", "/create", "/tn", nome, "/sc", "weekly",
        "/d", ",".join(giorni_schtasks), "/st", orario,
        "/tr", f'"{pythonw}" "{percorso_script}"',
        "/f",
    ]
    if _processo_e_elevato():
        comando += ["/rl", "highest"]
    risultato = subprocess.run(comando, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return risultato.returncode == 0, (risultato.stderr.strip() or risultato.stdout.strip())


def _elimina_task(nome: str) -> tuple:
    risultato = subprocess.run(
        ["schtasks", "/delete", "/tn", nome, "/f"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return risultato.returncode == 0, (risultato.stderr.strip() or risultato.stdout.strip())


# ----------------------------------------------------------------------
# Lancio di Schede Luci (tool separato per l'inventario schede/canali/
# alimentazione). Il percorso di default assume che viva accanto a questa
# cartella, ma puo' essere spostato altrove: in tal caso si aggiorna con
# il pulsante "..." accanto a SCHEDE LUCI, che salva il percorso scelto
# in CONFIGURAZIONE_SHOW.ini ([PERCORSI] Percorso_Schede_Luci) cosi' non
# serve piu' toccare il codice quando la cartella cambia posizione.
# ----------------------------------------------------------------------
PERCORSO_SCHEDE_LUCI_DEFAULT = CARTELLA_AUTOMAZIONE.parent / "04_SCHEDE_LUCI" / "SchedeLuci.pyw"


def percorso_schede_luci() -> Path:
    parser = carica_parser()
    grezzo = parser.get("PERCORSI", "Percorso_Schede_Luci", fallback="").strip()
    return Path(grezzo) if grezzo else PERCORSO_SCHEDE_LUCI_DEFAULT


def apri_schede_luci() -> None:
    percorso = percorso_schede_luci()
    if not percorso.exists():
        messagebox.showerror(
            "Schede Luci non trovato",
            f"Non trovo {percorso}\n\nUsa il pulsante \"...\" per indicare dove si trova adesso.",
        )
        return
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(percorso)], cwd=str(percorso.parent))


def cambia_percorso_schede_luci() -> None:
    percorso_attuale = percorso_schede_luci()
    scelto = filedialog.askopenfilename(
        title="Indica dove si trova SchedeLuci.pyw",
        initialdir=str(percorso_attuale.parent if percorso_attuale.parent.is_dir() else percorso_attuale.anchor),
        filetypes=[("Schede Luci", "SchedeLuci.pyw"), ("Tutti i file", "*.*")],
    )
    if not scelto:
        return
    parser = carica_parser()
    parser["PERCORSI"]["Percorso_Schede_Luci"] = scelto
    salva_parser(parser)
    messagebox.showinfo("Percorso aggiornato", f"Schede Luci verra' cercato qui:\n{scelto}")


def _calcola_orario_anticipato(orario_hhmm: str, minuti_anticipo: int) -> str:
    base = datetime.strptime(orario_hhmm.strip(), "%H:%M")
    anticipato = base - timedelta(minutes=minuti_anticipo)
    return anticipato.strftime("%H:%M")


# ----------------------------------------------------------------------
# Finestra di dialogo per Aggiungi/Modifica canzone
# ----------------------------------------------------------------------
class DialogoCanzone(tk.Toplevel):
    def __init__(self, padre, cartella_sequenze: Path, lingua: str, dati_esistenti: dict = None):
        super().__init__(padre)
        self.cartella_sequenze = cartella_sequenze
        self.lingua = lingua
        self.dati_esistenti = dati_esistenti
        self.risultato_salvato = False

        self.title(
            testo(lingua, "gc_titolo_dialogo_modifica") if dati_esistenti
            else testo(lingua, "gc_titolo_dialogo_aggiungi")
        )
        self.resizable(False, False)
        self.transient(padre)
        self.grab_set()

        self.percorso_audio_scelto = dati_esistenti["percorso_audio"] if dati_esistenti else None
        self.percorso_copertina_scelta = dati_esistenti["percorso_copertina"] if dati_esistenti else None

        self._costruisci_campi()

    def _t(self, chiave: str, **valori) -> str:
        return testo(self.lingua, chiave, **valori)

    def _costruisci_campi(self):
        padding = {"padx": 8, "pady": 4}
        modifica = self.dati_esistenti is not None

        ttk.Label(self, text=self._t("gc_campo_nome")).grid(row=0, column=0, sticky="w", **padding)
        self.var_nome = tk.StringVar(value=self.dati_esistenti["cartella"].name if modifica else "")
        entry_nome = ttk.Entry(self, textvariable=self.var_nome, width=40)
        entry_nome.grid(row=0, column=1, columnspan=2, sticky="w", **padding)

        ttk.Label(self, text=self._t("gc_campo_audio")).grid(row=1, column=0, sticky="w", **padding)
        self.var_audio = tk.StringVar(
            value=self.percorso_audio_scelto.name if self.percorso_audio_scelto else self._t("gc_nessuno_scelto")
        )
        ttk.Label(self, textvariable=self.var_audio, width=32).grid(row=1, column=1, sticky="w", **padding)
        ttk.Button(self, text=self._t("gc_sfoglia_audio"), command=self._sfoglia_audio).grid(row=1, column=2, **padding)

        ttk.Label(self, text=self._t("gc_campo_titolo")).grid(row=2, column=0, sticky="w", **padding)
        self.var_titolo = tk.StringVar(value=self.dati_esistenti["titolo"] if modifica else "")
        ttk.Entry(self, textvariable=self.var_titolo, width=40).grid(row=2, column=1, columnspan=2, sticky="w", **padding)

        ttk.Label(self, text=self._t("gc_campo_artista")).grid(row=3, column=0, sticky="w", **padding)
        self.var_artista = tk.StringVar(value=self.dati_esistenti["artista"] if modifica else "")
        ttk.Entry(self, textvariable=self.var_artista, width=40).grid(row=3, column=1, columnspan=2, sticky="w", **padding)

        ttk.Label(self, text=self._t("gc_campo_copertina")).grid(row=4, column=0, sticky="w", **padding)
        self.var_copertina = tk.StringVar(
            value=self.percorso_copertina_scelta.name if self.percorso_copertina_scelta else self._t("gc_nessuna")
        )
        ttk.Label(self, textvariable=self.var_copertina, width=32).grid(row=4, column=1, sticky="w", **padding)
        ttk.Button(self, text=self._t("gc_sfoglia_copertina"), command=self._sfoglia_copertina).grid(row=4, column=2, **padding)

        ttk.Label(self, text=self._t("gc_campo_trigger")).grid(row=5, column=0, sticky="w", **padding)
        trigger_attuale = ""
        if modifica:
            parser = carica_parser()
            trigger_attuale = leggi_trigger(parser, self.dati_esistenti["id"])
        self.var_trigger = tk.StringVar(value=trigger_attuale)
        self.combo_trigger = ttk.Combobox(
            self, textvariable=self.var_trigger, width=37, values=self._genera_suggerimenti_trigger(),
        )
        self.combo_trigger.grid(row=5, column=1, columnspan=2, sticky="w", **padding)

        ttk.Label(self, text=self._t("gc_campo_nome_lor")).grid(row=6, column=0, sticky="w", **padding)
        nome_lor_attuale = ""
        if modifica:
            parser = carica_parser()
            nome_lor_attuale = leggi_nome_lor(parser, self.dati_esistenti["id"])
        self.var_nome_lor = tk.StringVar(value=nome_lor_attuale)
        self.combo_nome_lor = ttk.Combobox(self, textvariable=self.var_nome_lor, width=37, values=())
        self.combo_nome_lor.grid(row=6, column=1, sticky="w", **padding)
        ttk.Button(self, text=self._t("gc_bottone_carica_da_lor"), command=self._carica_sequenze_lor).grid(row=6, column=2, **padding)

        frame_bottoni = ttk.Frame(self)
        frame_bottoni.grid(row=7, column=0, columnspan=3, sticky="e", padx=8, pady=10)
        ttk.Button(frame_bottoni, text=self._t("gc_bottone_annulla"), command=self.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(frame_bottoni, text=self._t("gc_bottone_salva"), command=self._salva).grid(row=0, column=1, padx=4)

        entry_nome.focus_set()

    def _sfoglia_audio(self):
        percorso = filedialog.askopenfilename(
            title=self._t("gc_scegli_audio"),
            filetypes=[
                (self._t("gc_filtro_audio"), " ".join(f"*{e}" for e in cat.ESTENSIONI_AUDIO)),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if not percorso:
            return
        self.percorso_audio_scelto = Path(percorso)
        self.var_audio.set(self.percorso_audio_scelto.name)

        if not self.var_titolo.get().strip() and not self.var_artista.get().strip():
            titolo, artista, _ = cat.leggi_metadati_audio(self.percorso_audio_scelto, self.percorso_audio_scelto.stem)
            self.var_titolo.set(titolo)
            self.var_artista.set(artista)

    def _sfoglia_copertina(self):
        percorso = filedialog.askopenfilename(
            title=self._t("gc_scegli_copertina"),
            filetypes=[
                (self._t("gc_filtro_immagini"), " ".join(f"*{e}" for e in cat.ESTENSIONI_IMMAGINE)),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if percorso:
            self.percorso_copertina_scelta = Path(percorso)
            self.var_copertina.set(self.percorso_copertina_scelta.name)

    def _genera_suggerimenti_trigger(self) -> list:
        """Genera in locale una lista di codici 'Regular-01-N' crescenti da
        proporre nella tendina del trigger: non c'e' nessuna API di LOR che
        elenchi i trigger esistenti, quindi la lista si basa sul numero di
        sequenze gia' salvate in 02_SEQUENZE (+ qualche numero libero in
        piu'), cosi' non serve scriverli a mano."""
        try:
            numero_sequenze = sum(1 for p in self.cartella_sequenze.iterdir() if p.is_dir())
        except OSError:
            numero_sequenze = 0
        ultimo = max(numero_sequenze, 1) + 3
        return [f"Regular-01-{i}" for i in range(1, ultimo + 1)]

    def _carica_sequenze_lor(self):
        parser = carica_parser()
        try:
            nomi = leggi_sequenze_da_lor(parser)
        except RuntimeError as errore:
            messagebox.showerror(
                self._t("gc_lor_non_raggiungibile_titolo"),
                self._t("gc_lor_non_raggiungibile_testo", errore=errore),
            )
            return

        if not nomi:
            messagebox.showinfo(self._t("gc_lor_nessuna_sequenza_titolo"), self._t("gc_lor_nessuna_sequenza_testo"))
            return

        self.combo_nome_lor.configure(values=nomi)
        self.combo_nome_lor.tk.call("ttk::combobox::Post", self.combo_nome_lor)

    def _salva(self):
        nome = self.var_nome.get().strip()
        titolo = self.var_titolo.get().strip()
        artista = self.var_artista.get().strip()
        trigger = self.var_trigger.get().strip()
        nome_lor = self.var_nome_lor.get().strip()
        modifica = self.dati_esistenti is not None

        if not nome:
            messagebox.showerror(self._t("gc_errore_campo_obbligatorio"), self._t("gc_errore_nome_vuoto"))
            return
        if not titolo:
            messagebox.showerror(self._t("gc_errore_campo_obbligatorio"), self._t("gc_errore_titolo_vuoto"))
            return

        cartella_originale = self.dati_esistenti["cartella"] if modifica else None
        rinominata = modifica and cartella_originale.name != nome
        cartella_destinazione = self.cartella_sequenze / nome

        if (not modifica or rinominata) and cartella_destinazione.exists():
            messagebox.showerror(self._t("gc_nome_gia_usato_titolo"), self._t("gc_nome_gia_usato_testo", nome=nome))
            return
        if not modifica and self.percorso_audio_scelto is None:
            messagebox.showerror(self._t("gc_audio_mancante_titolo"), self._t("gc_audio_mancante_testo"))
            return

        if rinominata and not messagebox.askyesno(
            self._t("gc_conferma_rinomina_titolo"),
            self._t("gc_conferma_rinomina_testo", vecchio=cartella_originale.name, nuovo=nome),
        ):
            return

        try:
            if rinominata:
                cartella_originale.rename(cartella_destinazione)
                if self.dati_esistenti.get("percorso_audio") is not None:
                    self.dati_esistenti["percorso_audio"] = cartella_destinazione / self.dati_esistenti["percorso_audio"].name
                if self.dati_esistenti.get("percorso_copertina") is not None:
                    self.dati_esistenti["percorso_copertina"] = cartella_destinazione / self.dati_esistenti["percorso_copertina"].name

            cartella_destinazione.mkdir(parents=True, exist_ok=True)

            audio_precedente = self.dati_esistenti.get("percorso_audio") if modifica else None
            if not modifica or self.percorso_audio_scelto != audio_precedente:
                if self.percorso_audio_scelto is not None:
                    file_audio_finale = cartella_destinazione / self.percorso_audio_scelto.name
                    if self.percorso_audio_scelto != file_audio_finale:
                        shutil.copyfile(self.percorso_audio_scelto, file_audio_finale)
                    if audio_precedente is not None and audio_precedente != file_audio_finale and audio_precedente.exists():
                        audio_precedente.unlink()
                else:
                    file_audio_finale = None
            else:
                file_audio_finale = audio_precedente

            if file_audio_finale is not None:
                cat.scrivi_metadati_audio(file_audio_finale, titolo, artista)

            copertina_precedente = self.dati_esistenti.get("percorso_copertina") if modifica else None
            if self.percorso_copertina_scelta is not None and self.percorso_copertina_scelta != copertina_precedente:
                if copertina_precedente is not None and copertina_precedente.exists():
                    copertina_precedente.unlink()

                file_copertina_finale = cartella_destinazione / (
                    "copertina" + self.percorso_copertina_scelta.suffix.lower()
                )
                if self.percorso_copertina_scelta != file_copertina_finale:
                    shutil.copyfile(self.percorso_copertina_scelta, file_copertina_finale)

            id_canzone = cat.crea_id(nome)
            parser = carica_parser()

            if rinominata:
                vecchio_id = cat.crea_id(cartella_originale.name)
                trigger_precedente = leggi_trigger(parser, vecchio_id)
                scrivi_trigger(parser, vecchio_id, "")
                scrivi_nome_lor(parser, vecchio_id, "")

                cartelle_escluse = leggi_cartelle_escluse(parser)
                if any(c.lower() == cartella_originale.name.lower() for c in cartelle_escluse):
                    cartelle_escluse = {c for c in cartelle_escluse if c.lower() != cartella_originale.name.lower()}
                    cartelle_escluse.add(nome)
                    scrivi_cartelle_escluse(parser, cartelle_escluse)
            else:
                trigger_precedente = leggi_trigger(parser, id_canzone)

            scrivi_trigger(parser, id_canzone, trigger)
            scrivi_nome_lor(parser, id_canzone, nome_lor)
            salva_parser(parser)

            ok, errore = rigenera_catalogo_sicuro(self.lingua)
            if not ok:
                messagebox.showwarning(
                    self._t("gc_salvato_con_avvisi_titolo"),
                    self._t("gc_salvato_con_avvisi_testo", errore=errore),
                )

            if trigger and trigger != trigger_precedente:
                messagebox.showinfo(
                    self._t("gc_promemoria_titolo"),
                    self._t("gc_promemoria_testo", trigger=trigger, titolo=titolo, nome=nome),
                )

            self.risultato_salvato = True
            self.destroy()

        except OSError as errore:
            messagebox.showerror(self._t("gc_errore_titolo"), self._t("gc_errore_salvataggio_file", errore=errore))


# ----------------------------------------------------------------------
# Finestra Guida (companion window agganciata alla finestra principale)
# ----------------------------------------------------------------------
TESTO_GUIDA = {
    "it": {
        "titolo": "Guida - Programmazione",
        "corpo": """COSA FA QUESTA SCHEDA

STATO ATTUALE
Due pallini mostrano se VotoShow.py (il server di voto) e MotoreShow.py (il controllo pre-volo) sono in esecuzione in questo momento: verde = acceso, rosso = spento. "Avvia ora"/"Stop ora" fanno partire o fermare SOLO il sistema di voto, a mano, senza aspettare l'orario programmato - utile per test o emergenze. IMPORTANTE: questi pulsanti non toccano LOR in nessun modo - non accendono ne' spengono le luci/la musica, che LOR gestisce sempre da solo secondo il proprio orario interno, indipendente da questa GUI.

"Voto", "Benvenuto" e "Apri risultati" (accanto ad "Avvia ora"/"Stop ora") aprono nel browser rispettivamente la pagina di richiesta che vedono gli ospiti dal telefono, la pagina di benvenuto e la pagina con la classifica/risultati del voto in corso - utili per controllare al volo come si vedono senza dover usare un altro telefono. Funzionano SOLO se il pallino di VotoShow.py e' verde in quel momento: se e' rosso, le pagine non si caricano perche' non c'e' nessun server ad aspettarle.

"Test multi-voto" (un po' piu' staccato, a destra) apre in un colpo solo piu' finestre del browser, ciascuna con un profilo tutto suo (cookie separati): ognuna conta come un elettore diverso, senza dover aprire browser diversi a mano per simulare piu' telefoni. Chiede quante finestre aprire, poi le apre gia' pronte sulla pagina di voto. Serve Chrome o Edge installati in un percorso standard di Windows. Solo per queste finestre locali (indirizzo 127.0.0.1) il limite anti-spam di 2 secondi tra un voto e l'altro e' disattivato, cosi' puoi votare velocemente da tutte senza bloccarti a vicenda - un ospite vero da telefono non puo' mai sfruttare questa esenzione, vale solo per il traffico generato dal PC stesso.

ATTENZIONE - ManagerShow e VotoShow sono DUE programmi separati e indipendenti. Chiudere e riaprire questa finestra (ManagerShow) NON tocca VotoShow in nessun modo: se era acceso resta acceso esattamente come prima, il pallino verde dice solo la verita' su quel processo, che va avanti per conto suo. Se hai cambiato qualcosa nella configurazione che VotoShow deve rileggere (es. il campo "Nome in LOR" di una canzone, nella scheda Canzoni), quella modifica NON si applica da sola: serve fermarlo e farlo ripartire davvero con "Stop ora" e poi "Avvia ora" (o aspettare il prossimo avvio pianificato). VotoShow.py legge la sua configurazione e il catalogo canzoni SOLO all'avvio, mai mentre gira.

ORARI
Orario in cui il sistema di voto (VotoShow.py) deve accendersi/spegnersi da solo ogni giorno. IMPORTANTE: deve combaciare con lo show luminoso di LOR (quello dentro Sequencer/Show Player) - sono due pianificazioni separate (questa qui, del sistema di voto, e quella dentro LOR, dello show vero e proprio) che vanno tenute allineate a mano. Meglio mettere l'Accensione qualche minuto PRIMA dell'orario schedulato in LOR (vedi il box Pulizia qui sotto): cosi' il sistema di voto e' gia' su e pronto quando lo show di LOR parte davvero, invece di rischiare che le prime canzoni partano senza che gli ospiti possano ancora votare.

GIORNI ATTIVI
In quali giorni della settimana il sistema di voto deve accendersi da solo. Se un giorno non e' spuntato, quel giorno VotoShow.py non parte anche se dentro l'orario configurato - controlla che combaci con i giorni in cui lo show di LOR e' programmato per andare in onda.

PULIZIA AUTOMATICA (PRE-SHOW LOR - PRE-VOTO)
Il PC si pulisce da solo (Chrome, cache/temp, programmi che scegli tu) un certo numero di minuti PRIMA dell'Orario Accensione - quanti minuti lo decidi qui. Questo pero' succede in automatico SOLO se hai premuto "Pianifica Win Auto" nel box qui sotto: se non l'hai premuto, la pulizia automatica non parte mai da sola. Il pulsante "Pulisci ora" e' invece un'azione separata e solo manuale, sempre disponibile su richiesta. Per tutti i dettagli vedi il pulsante "Guida pulizia" dentro quel box.

STATO PIANIFICAZIONE
Mostra se la pianificazione di Windows e' attiva. "Salva orari" scrive gli orari nell'ini senza toccare Windows. "Pianifica Win Auto" registra 3 attivita' nella Utilita' di Pianificazione di Windows che faranno partire/fermare da sole il sistema di voto agli orari scelti (LOR resta sempre gestito a parte, dal proprio schedule interno); una volta attiva, lo stesso pulsante diventa "Rimuovi Win Auto" per cancellare quelle 3 attivita' (il sistema di voto smette di partire da solo, ma non cancella nulla dal disco, e LOR continua a funzionare come sempre).

ATTENZIONE - LA SCRITTA VERDE PUO' MENTIRE SUL PERCORSO (non sul fatto che esista)
La scritta verde "attiva" controlla SOLO se le 3 attivita' esistono per nome dentro Windows - NON controlla se il percorso salvato al loro interno e' ancora quello giusto. Se in futuro sposti la cartella di AUTOMAZIONE o di SERVER_VOTO (o l'intera cartella che le contiene), le 3 attivita' restano li' e la scritta resta verde, ma puntano ancora al vecchio percorso: domani, all'orario previsto, falliranno in silenzio (il file che cercano non esiste piu' li'). QUINDI: ogni volta che sposti quelle cartelle, anche se la scritta e' gia' verde, premi comunque "Rimuovi Win Auto" e poi SUBITO DOPO "Pianifica Win Auto" di nuovo - sono due click in sequenza, il primo cancella le 3 attivita' vecchie, il secondo le ricrea da zero con il percorso corretto (ricalcolato automaticamente in base a dove si trova ManagerShow.pyw in quel momento).

IMPORTANTE: scrivere gli orari nei campi qui sopra (anche con "Salva orari") non fa scattare nulla da solo - e' solo un dato salvato. E anche "Pianifica Win Auto" non ha nessun effetto immediato: registra il task per il FUTURO, al prossimo orario utile. Se lo premi quando l'orario di Spegnimento di oggi e' gia' passato, il sistema di voto NON si ferma subito - restera' acceso come adesso e si fermera' da solo al prossimo orario schedulato (es. stasera, se oggi e' tra i Giorni Attivi). Per fermarlo SUBITO serve comunque "Stop ora", sono due azioni separate.


--- GUIDA TECNICA (come gira davvero) ---

Ogni orario in questa scheda viene scritto in CONFIGURAZIONE_SHOW.ini (sezioni [ORARI] e [PULIZIA]), il file centrale che tutti gli script Python rileggono a ogni avvio.

"Pianifica Win Auto" chiama 'schtasks' di Windows per creare 3 attivita' pianificate, nei giorni della settimana selezionati:
  - V1965_Show_Pulizia -> esegue PuliziaPreShow.py all'orario calcolato (accensione meno i minuti di anticipo)
  - V1965_Show_Avvio   -> esegue AvviaShow.py all'orario di accensione
  - V1965_Show_Stop    -> esegue FermaShow.py all'orario di spegnimento

AvviaShow.py fa partire due processi: MotoreShow.py (una sentinella pre-volo: controlla che tutto sia pronto, poi si chiude da sola, non resta in esecuzione) e VotoShow.py (il vero server di voto, resta acceso finche' non arriva FermaShow.py o lo fermi tu a mano).

VotoShow.py fa due cose in parallelo: (1) espone su http://<ip>:8080 la pagina di benvenuto (configurabile nella scheda "Benvenuto") che porta alla pagina di richiesta vera e propria su /vota; (2) tiene un thread ("ponte con LOR") che ogni 3 secondi chiede all'API REST di LOR (porta 8001, nessuna configurazione extra serve dentro LOR) quale sequenza sta suonando e quanto manca alla fine.

Le richieste sono cumulative: ogni telefono puo' cambiare richiesta quante volte vuole (vale l'ultima inviata), e quando mancano 6 secondi alla fine si conta quante persone hanno chiesto ciascuna sequenza in questo turno - vince quella con piu' richieste (in caso di pareggio, vince chi tra i pari merito ha la richiesta attiva piu' vecchia; solo con zero richieste non si fa nulla e la scaletta normale di LOR prosegue).

Quando c'e' un vincitore chiaro, VotoShow.py accoda anche il resto della classifica di quel turno (es. 5 richieste per la prima, 3 per la seconda, 2 per la terza) IN FONDO alla coda gia' esistente, senza scartare quello che c'era prima: chi ha vinto un turno precedente e sta ancora aspettando in coda non perde mai il proprio posto per via di richieste piu' recenti - altrimenti, con richieste continue ogni turno, chi ha votato per la sequenza arrivata seconda/terza non la vedrebbe mai partire. Ad ogni fine turno la coda va comunque avanti: si manda in playNext la prima voce non in pausa trovata in coda (quelle in pausa restano al loro posto per un turno successivo).

RETE E ACCESSO OSPITI
Il telefono degli ospiti deve stare sulla STESSA rete WiFi di casa del PC che fa girare VotoShow.py (raggiungere l'IP del PC sulla porta 8080). Se il router ha una rete "ospiti" separata va bene usarla, ma occhio alla "client isolation" (isolamento dei dispositivi tra loro): se e' attiva, il telefono non riesce a raggiungere il PC anche se e' sulla rete giusta - va disattivata per questo uso. Meglio se la rete ospiti e' aperta (senza password): chi passa vicino a casa si collega subito senza doverti chiedere nulla. Per non far scrivere l'indirizzo IP a mano su ogni telefono, il modo piu' comodo e' generare un codice QR che punta a http://<ip>:8080 (con un generatore QR gratuito online) e stamparlo o appenderlo vicino allo show: gli ospiti inquadrano il QR con la fotocamera, vedono prima la pagina di benvenuto e da li' passano al voto vero e proprio con un tocco. Alcuni router (es. Fritz!Box) possono anche reindirizzare da soli chi si connette al WiFi ospiti su questo stesso indirizzo, senza bisogno del QR.

I pallini di stato qui sopra controllano solo se un processo con quel nome (VotoShow.py / MotoreShow.py) e' in esecuzione su Windows in questo momento - non parlano con l'API di LOR, solo con l'elenco processi del sistema operativo.

VERSIONE DI LOR SUITE TESTATA - questo programma e' stato sviluppato e verificato dal vivo su **LOR Suite 6.2.0.18** (Sequencer/Show Player e SuperStar, stessa build). Gli endpoint REST API usati (mainSequences, playNext, player/stop, ecc.) sono quelli documentati nel manuale ufficiale, quindi dovrebbe funzionare anche su versioni vicine - ma non e' stato verificato su altre versioni. Se lo usi su una versione diversa e qualcosa non funziona (o funziona!), una segnalazione sulla pagina del progetto e' preziosa per tutti.

ACCESSO DA FUORI CASA (dominio pubblico) - NON ANCORA DISPONIBILE, in programma per il futuro. Oggi il voto funziona SOLO sulla rete WiFi locale, per scelta precisa di sicurezza (vedi sopra "RETE E ACCESSO OSPITI"). Alcuni strumenti commerciali della community (es. MIIP) offrono gia' l'accesso da un dominio pubblico, raggiungibile da ospiti non sulla tua rete - usano un piccolo programma locale che si collega LUI verso il loro server (connessione in uscita), mai il contrario, cosi' non serve aprire porte sul router ne' esporre l'API di LOR a internet. Se un giorno implementeremo qualcosa di simile, seguira' lo stesso principio - e restera' comunque facoltativo, chi preferisce il voto solo locale (come funziona oggi) non dovra' cambiare nulla.


--- QR CODE (scheda "QR Code") ---

COS'E' - genera due QR code pronti da stampare e appendere vicino allo show: uno per collegarsi al WiFi ospiti (il telefono lo fa da solo scansionandolo, senza che nessuno debba scrivere la password a mano), uno che apre direttamente la pagina di voto. Tutto creato SUL TUO PC: nessun sito esterno coinvolto, la password WiFi non viene mai mandata da nessuna parte.

COME COMPILARLO - scrivi nome rete (SSID), password e tipo di sicurezza (WPA/WEP/rete aperta) nel primo box. Nel secondo box l'indirizzo IP viene proposto in automatico (rilevato dalla rete di questo PC) - se hai piu' schede di rete (es. una scheda USB dedicata all'antenna WiFi ospiti) puo' capitare che venga rilevato l'IP sbagliato: controllalo e correggilo a mano se serve, premendo "Rileva di nuovo" dopo aver cambiato configurazione di rete.

DIPENDENZE - la prima volta che premi "Genera QR Code" viene chiesto il permesso di installare i pacchetti 'qrcode' e 'Pillow' (stesso meccanismo gia' visto per Pillow nella scheda Benvenuto) - si installano una volta sola.

SALVATAGGIO - "Salva immagine WiFi..."/"Salva immagine Voto..." esportano due file PNG separati, pronti da stampare o inserire in un volantino. I dati compilati (SSID, password, IP) restano salvati e si ricaricano riaprendo la scheda.


--- SALUTO DI BABBO NATALE (box "Saluto di Babbo Natale (opzionale)") ---

COS'E' - una funzione facoltativa pensata per chi vuole qualcosa in piu' oltre al semplice voto delle canzoni: un genitore scrive il nome del proprio bambino dal telefono, e dopo la sequenza in corso (o quella successiva, a seconda del momento in cui arriva la richiesta) parte un saluto personalizzato - il nome del bambino appare animato su una prop luminosa dedicata, insieme a un audio con la voce di Babbo Natale che lo saluta.

E' VOLUTAMENTE opzionale e disattivata di default: e' pensata per essere piu' complessa da mettere in piedi rispetto al semplice voto canzoni (richiede una prop dedicata alla scritta, un account su un servizio esterno di sintesi vocale, e la generazione in anticipo o al volo del saluto per ogni nome) - se non ti interessa, lascia la spunta disattivata e per gli ospiti non cambia nulla: vedono solo la pagina di voto, come se questa funzione non esistesse.

COME FUNZIONA (lato ospite) - se abilitata, prima della pagina di voto compare un bivio: "Fai salutare il tuo bambino" oppure "Scegli la tua animazione preferita" (il voto di sempre). Nessuna delle due scelte esclude l'altra: si puo' fare prima una, poi tornare al menu e fare anche l'altra. Chi sceglie il saluto vede prima una breve spiegazione (compreso un avviso che, generandosi tutto al momento, potrebbero capitare rari intoppi tecnici), poi scrive il nome - puo' ripetere per piu' figli, uno alla volta.

COSA SERVE PER FARLA FUNZIONARE DAVVERO (non ancora tutto pronto) - oltre alla spunta qui: (1) una prop luminosa dedicata a mostrare il testo (es. una matrice di pixel), con la sua codifica pixel gia' verificata; (2) un account su un servizio di sintesi vocale (es. ElevenLabs) per generare l'audio della voce che pronuncia il nome; (3) il modulo che genera davvero l'animazione+audio per ogni nome (in sviluppo, vive separato da VotoShow.py cosi' e' riusabile anche altrove). Finche' questi pezzi non sono pronti, anche con la spunta attivata il saluto raccoglie il nome ma non genera ancora nulla di reale.

QUANDO SI APPLICA - come la modalita' coda, questa spunta viene letta da VotoShow.py solo all'avvio: se lo cambi mentre e' gia' acceso, serve "Stop ora" poi "Avvia ora" (te lo propone gia' il pulsante di salvataggio).


--- BENEFICENZA (scheda "Beneficenza") ---

COS'E' - una pagina di contenuto libero, raggiungibile dal menu ospiti con un link "❤️ Beneficenza", pensata per restare riconsultabile quando serve (a differenza della pagina di Benvenuto, vista una volta sola all'ingresso). Puoi usarla per una donazione, un ente benefico, una raccolta giocattoli, uno sponsor, le regole dello show - qualsiasi cosa vuoi.

VISIBILITA' - la spunta "Pagina visibile agli ospiti" e' l'UNICO interruttore che conta: se spuntata il link compare, anche se non hai ancora scritto nulla (gli ospiti vedono un messaggio generico "pagina in arrivo"); se NON spuntata, il link resta nascosto del tutto qualunque cosa tu abbia gia' compilato. Comodo per preparare i contenuti con calma prima di pubblicarli.

IL PULSANTE "DONA ORA" - se compili il campo "Link donazione" con un indirizzo web, sulla pagina compare un pulsante "❤️ Dona ora" che porta li' (si apre in una scheda nuova, il resto del sito non si interrompe). Come procurarti un indirizzo senza scrivere codice:
  - STRIPE: crea un account su stripe.com, poi un "Payment Link" (Pagamenti > Link di pagamento) - imposti l'importo (fisso o libero) e Stripe ti da' un URL pronto, incollalo qui.
  - PAYPAL: crea un link "PayPal.me" dal tuo account PayPal (paypal.me/TUONOME) - ancora piu' rapido, nessuna configurazione oltre al nome.
  - Entrambi i servizi trattengono una piccola commissione sulle transazioni - informati sulle condizioni aggiornate direttamente sui loro siti prima di scegliere.

Per qualcosa di piu' su misura (una pagina di pagamento con il tuo logo, tracciamento di chi ha donato, ecc.) serve invece scrivere codice: se non sei un programmatore, farti aiutare da un assistente AI (es. Claude, ChatGPT) per questa parte specifica e' un modo pienamente valido per cavartela comunque - gli basta spiegare cosa vuoi ottenere, mostrargli questo file (VotoShow.py) e i campi gia' pronti ([INFO] nell'ini: Link_Donazione, Testo_Grazie) da cui partire.

PAGINA DI RINGRAZIAMENTO - un indirizzo separato (/grazie) pensato per essere impostato come "pagina di ritorno dopo il pagamento" nel servizio scelto (sia Stripe Checkout sia PayPal supportano un indirizzo di ritorno personalizzato, cercalo nelle loro impostazioni come "success URL" o "return URL"). Se lasci il campo "Testo" di questa sezione vuoto, viene mostrato un ringraziamento generico gia' pronto - altrimenti il tuo testo personalizzato. Segue la stessa spunta "Visibile" qui sopra, nessun interruttore separato.


--- SCHEDE LUCI (il pulsante in cima alla finestra) ---

E' uno strumento completamente SEPARATO e indipendente da tutto il resto di questa app: serve per tenere l'inventario tecnico delle schede luci (canali, alimentazione, dove sono fisicamente installate). Non condivide nessun dato con canzoni/voto/pianificazione - e' come se fosse un altro programma, semplicemente lo lanci da qui per comodita'.

Puo' vivere in qualsiasi cartella, anche su un disco diverso: il pulsante "SCHEDE LUCI" lo cerca in un percorso salvato in CONFIGURAZIONE_SHOW.ini ([PERCORSI] Percorso_Schede_Luci). Se quel campo e' vuoto, prova un percorso calcolato di default (accanto alla cartella di questa app); se invece e' compilato, usa esattamente quel percorso, qualunque esso sia.

Se sposti la cartella di Schede Luci: premi il pulsante "..." accanto a "SCHEDE LUCI" e scegli di nuovo il file SchedeLuci.pyw nella sua nuova posizione. Non serve toccare nessun altro file ne' riavviare nulla: il campo si aggiorna subito nell'ini e da quel momento "SCHEDE LUCI" lo trova li'.""",
    },
    "en": {
        "titolo": "Guide - Scheduling",
        "corpo": """WHAT THIS TAB DOES

CURRENT STATUS
Two dots show whether VotoShow.py (the voting server) and MotoreShow.py (the pre-flight check) are currently running: green = on, red = off. "Start now"/"Stop now" start or stop ONLY the voting system, manually, without waiting for the scheduled time - useful for testing or emergencies. IMPORTANT: these buttons don't touch LOR in any way - they don't turn the lights/music on or off, LOR always manages that on its own, on its own internal schedule, independent of this GUI.

"Vote", "Welcome" and "Open results" (next to "Start now"/"Stop now") open in the browser, respectively, the guest request page (the same one guests see on their phone), the welcome page, and the current voting results/ranking page - handy for checking how they look without needing another phone. They only work if VotoShow.py's dot is green right now: if it's red, the pages won't load because there's no server waiting for them.

"Multi-vote test" (set slightly apart, to the right) opens several browser windows at once, each with its own separate profile (separate cookies): each one counts as a different voter, without having to open different browsers by hand to simulate several phones. It asks how many windows to open, then opens them already pointed at the voting page. Requires Chrome or Edge installed in a standard Windows location. Only for these local windows (address 127.0.0.1) the 2-second anti-spam limit between votes is disabled, so you can vote quickly from all of them without blocking each other - a real guest on their phone can never exploit this exemption, it only applies to traffic generated by the PC itself.

WARNING - ManagerShow and VotoShow are TWO separate, independent programs. Closing and reopening this window (ManagerShow) does NOT touch VotoShow in any way: if it was running, it keeps running exactly as before, the green dot is just telling the truth about that process, which carries on by itself. If you changed something in the configuration that VotoShow needs to re-read (e.g. a song's "Name in LOR" field, in the Songs tab), that change does NOT apply on its own: you need to actually stop it and start it again with "Stop now" then "Start now" (or wait for the next scheduled start). VotoShow.py reads its configuration and song catalog ONLY at startup, never while running.

SCHEDULE
Start and stop time for the voting system (VotoShow.py) to turn itself on/off every day. IMPORTANT: this must match LOR's own light show (the one inside Sequencer/Show Player) - these are two separate schedules (this one, for the voting system, and LOR's own, for the actual show) that must be kept aligned by hand. It's best to set Start a few minutes BEFORE the time scheduled in LOR (see the Cleanup box below): that way the voting system is already up and ready when LOR's show actually starts, instead of risking the first songs playing before guests can vote yet.

ACTIVE DAYS
Which days of the week the voting system should turn on by itself. If a day is unchecked, VotoShow.py doesn't start that day even within the configured hours - make sure it matches the days LOR's show is scheduled to run.

AUTOMATIC CLEANUP (PRE-SHOW LOR - PRE-VOTE)
The PC cleans itself (Chrome, cache/temp, programs you choose) a number of minutes BEFORE Start time - you choose how many here. This only happens automatically if you've pressed "Schedule Win Auto" in the box below: if you haven't, automatic cleanup never runs on its own. The "Clean now" button is a separate, manual-only action, always available on demand. See the "Cleanup guide" button inside that box for all the details.

SCHEDULE STATUS
Shows whether Windows scheduling is active. "Save times" writes the times to the ini without touching Windows. "Schedule Win Auto" registers 3 tasks in Windows Task Scheduler that will start/stop the voting system on their own at the chosen times (LOR is always managed separately, by its own internal schedule); once active, the same button becomes "Remove Win Auto" to delete those 3 tasks (the voting system stops starting on its own, but nothing is deleted from disk, and LOR keeps working as usual).

WARNING - THE GREEN LABEL CAN LIE ABOUT THE PATH (not about whether it exists)
The green "active" label only checks whether the 3 tasks exist by name inside Windows - it does NOT check whether the path saved inside them still points to the right place. If you later move the AUTOMAZIONE or SERVER_VOTO folder (or the whole folder containing them), the 3 tasks stay there and the label stays green, but they still point to the old path: tomorrow, at the scheduled time, they will silently fail (the file they're looking for no longer exists there). SO: every time you move those folders, even if the label is already green, press "Remove Win Auto" anyway and then IMMEDIATELY AFTER press "Schedule Win Auto" again - two clicks in a row, the first deletes the 3 old tasks, the second recreates them from scratch with the correct path (recalculated automatically based on where ManagerShow.pyw currently is).

IMPORTANT: typing times in the fields above (even with "Save times") doesn't trigger anything by itself - it's just saved data. And "Schedule Win Auto" has no immediate effect either: it registers the task for the FUTURE, at the next matching time. If you press it when today's Stop time has already passed, the voting system does NOT stop right away - it stays on as it is now and will stop on its own at the next scheduled time (e.g. tonight, if today is one of the Active Days). To stop it RIGHT NOW you still need "Stop now" - they are two separate actions.


--- TECHNICAL GUIDE (how it actually runs) ---

Every time in this tab is written to CONFIGURAZIONE_SHOW.ini (sections [ORARI] and [PULIZIA]), the central file every Python script re-reads on every start.

"Schedule Win Auto" calls Windows 'schtasks' to create 3 scheduled tasks, on the selected weekdays:
  - V1965_Show_Pulizia -> runs PuliziaPreShow.py at the computed time (start time minus the lead minutes)
  - V1965_Show_Avvio   -> runs AvviaShow.py at start time
  - V1965_Show_Stop    -> runs FermaShow.py at stop time

AvviaShow.py starts two processes: MotoreShow.py (a one-shot pre-flight sentinel: checks everything is ready, then closes itself, it does not stay running) and VotoShow.py (the actual voting server, stays on until FermaShow.py runs or you stop it manually).

VotoShow.py does two things in parallel: (1) serves at http://<ip>:8080 the welcome page (configurable in the "Welcome" tab) that leads to the actual request page at /vota; (2) runs a thread (the "LOR bridge") that every 3 seconds asks LOR's REST API (port 8001, no extra setup needed inside LOR) which sequence is playing and how much time is left.

Requests are cumulative: each phone can change its request as many times as it wants (the last one sent counts), and when 6 seconds remain it counts how many people asked for each sequence this turn - the one with the most requests wins (on a tie, whoever among the tied entries has the oldest active request wins; only with zero requests does nothing happen and LOR's normal playlist continues).

When there's a clear winner, VotoShow.py also queues the rest of that turn's ranking (e.g. 5 requests for the first, 3 for the second, 2 for the third) onto the END of the queue that's already there, without discarding what came before: whoever won an earlier turn and is still waiting in the queue never loses their spot because of more recent requests - otherwise, with continuous requests every turn, whoever voted for the sequence that came second/third would never see it play. At the end of every turn the queue still moves forward: the first non-paused entry found in the queue gets sent to playNext (paused ones stay in place for a later turn).

NETWORK AND GUEST ACCESS
Guests' phones must be on the SAME home WiFi network as the PC running VotoShow.py (able to reach the PC's IP on port 8080). If the router has a separate "guest" network that's fine to use, but watch out for "client isolation" (devices kept from reaching each other): if it's on, the phone can't reach the PC even though it's on the right network - turn it off for this to work. It's best if the guest network is open (no password): anyone nearby connects right away without having to ask you anything. To avoid guests typing the IP address by hand on every phone, the easiest way is to generate a QR code pointing to http://<ip>:8080 (using any free online QR generator) and print it or hang it near the show: guests scan it with their phone camera, see the welcome page first, then move on to the actual vote with one tap. Some routers (e.g. Fritz!Box) can also redirect guest WiFi connections to this same address automatically, without needing the QR code.

The status dots above only check whether a process with that name (VotoShow.py / MotoreShow.py) is currently running on Windows - they don't talk to LOR's API, only to the operating system's process list.

LOR SUITE VERSION TESTED - this program was developed and verified live against **LOR Suite 6.2.0.18** (Sequencer/Show Player and SuperStar, same build). The REST API endpoints it uses (mainSequences, playNext, player/stop, etc.) are the ones documented in the official manual, so it should hold up on nearby versions too - but it hasn't been verified on other versions. If you run it on a different version and something works (or doesn't!), a report on the project page is valuable for everyone.

ACCESS FROM OUTSIDE HOME (public domain) - NOT AVAILABLE YET, planned for the future. Today voting only works on the local guest WiFi, by deliberate security choice (see "NETWORK AND GUEST ACCESS" above). Some commercial tools in this community (e.g. MIIP) already offer access from a public domain, reachable by guests not on your network - they use a small local program that connects OUTWARD to their server (never the other way around), so no router ports need opening and LOR's local API is never exposed to the internet. If we ever build something similar, it will follow the same principle - and will stay optional: anyone happy with local-only voting (as it works today) won't have to change anything.


--- QR CODE (the "QR Code" tab) ---

WHAT IT IS - generates two QR codes ready to print and hang near the show: one to join the guest WiFi (the phone does it on its own by scanning it, nobody needs to type the password by hand), one that opens the voting page directly. Both created ON YOUR OWN PC: no external site involved, the WiFi password is never sent anywhere.

HOW TO FILL IT IN - type the network name (SSID), password and security type (WPA/WEP/open network) in the first box. In the second box the IP address is suggested automatically (detected from this PC's network) - if you have more than one network adapter (e.g. a dedicated USB adapter for the guest WiFi antenna) it can sometimes detect the wrong one: check it and correct it by hand if needed, pressing "Detect again" after changing your network setup.

DEPENDENCIES - the first time you press "Generate QR Codes" you'll be asked permission to install the 'qrcode' and 'Pillow' packages (same mechanism already seen for Pillow in the Welcome tab) - installed once and done.

SAVING - "Save WiFi image..."/"Save Voting image..." export two separate PNG files, ready to print or put on a flyer. The filled-in data (SSID, password, IP) stays saved and reloads when you reopen the tab.


--- SANTA'S GREETING (the "Santa's greeting (optional)" box) ---

WHAT IT IS - an optional feature for anyone who wants something more than plain song voting: a parent types their child's name on their phone, and after the current sequence (or the next one, depending on when the request arrives) a personalized greeting plays - the child's name appears animated on a dedicated light prop, together with an audio clip of Santa's voice greeting them.

It is INTENTIONALLY optional and off by default: it's meant to be more involved to set up than plain song voting (it needs a dedicated prop for the text, an account with an external text-to-speech service, and generating the greeting for each name ahead of time or on the fly) - if you don't want it, leave the checkbox off and nothing changes for guests: they just see the voting page, as if this feature didn't exist.

HOW IT WORKS (guest side) - if enabled, before the voting page a fork appears: "Get a greeting from Santa" or "Pick your favorite animation" (the usual vote). Neither choice excludes the other: a guest can do one, go back to the menu, and do the other too. Whoever picks the greeting sees a short explanation first (including a note that, since everything is generated on the spot, rare technical hiccups can happen), then types the name - they can repeat for more than one child, one at a time.

WHAT'S NEEDED TO ACTUALLY MAKE IT WORK (not everything is ready yet) - besides this checkbox: (1) a dedicated light prop to display the text (e.g. a pixel matrix), with its pixel encoding already verified; (2) an account with a text-to-speech service (e.g. ElevenLabs) to generate the voice audio pronouncing the name; (3) the module that actually generates the animation+audio for each name (under development, lives separately from VotoShow.py so it can be reused elsewhere too). Until those pieces are ready, even with the checkbox on, the greeting form collects the name but doesn't generate anything real yet.

WHEN IT TAKES EFFECT - like the queue mode, this checkbox is only read by VotoShow.py at startup: if you change it while already running, you need "Stop now" then "Start now" (the save button already offers to do this for you).


--- CHARITY (the "Charity" tab) ---

WHAT IT IS - a free-form content page, reachable from the guest menu with a "❤️ Charity" link, meant to stay revisitable whenever needed (unlike the Welcome page, seen only once on entry). Use it for a donation, a charity, a toy drive, a sponsor, show rules - whatever you want.

VISIBILITY - the "Page visible to guests" checkbox is the ONLY switch that matters: if checked, the link shows up even if you haven't written anything yet (guests see a generic "page coming soon" message); if NOT checked, the link stays fully hidden no matter what you've already filled in. Handy for preparing content at your own pace before publishing it.

THE "DONATE NOW" BUTTON - if you fill in the "Donation link" field with a web address, a "❤️ Donate now" button appears on the page linking there (opens in a new tab, the rest of the site stays open). How to get an address without writing code:
  - STRIPE: create an account at stripe.com, then a "Payment Link" (Payments > Payment links) - set the amount (fixed or open) and Stripe gives you a ready URL, paste it here.
  - PAYPAL: create a "PayPal.me" link from your PayPal account (paypal.me/YOURNAME) - even quicker, no setup beyond your name.
  - Both services keep a small fee on transactions - check their current terms on their own sites before choosing.

For something more custom (a payment page with your own logo, tracking who donated, etc.) you'll need to write code: if you're not a programmer, getting help from an AI assistant (e.g. Claude, ChatGPT) for this specific part is a fully valid way to get it done anyway - just explain what you want to achieve, show it this file (VotoShow.py) and the fields already prepared for it ([INFO] in the ini: Link_Donazione, Testo_Grazie) as a starting point.

THANK-YOU PAGE - a separate address (/grazie) meant to be set as the "return page after payment" in your chosen service (both Stripe Checkout and PayPal support a custom return address, look for it in their settings as "success URL" or "return URL"). If you leave this section's "Text" field empty, a ready-made generic thank-you message is shown - otherwise your custom text. Follows the same "Visible" checkbox above, no separate switch.


--- LIGHT BOARDS / SCHEDE LUCI (the button at the top of the window) ---

This is a completely SEPARATE, independent tool from the rest of this app: it keeps the technical inventory of light boards (channels, power supply, where they're physically installed). It shares no data with songs/voting/scheduling - it's basically a different program, you just launch it from here for convenience.

It can live in any folder, even on a different drive: the "SCHEDE LUCI" button looks for it at a path saved in CONFIGURAZIONE_SHOW.ini ([PERCORSI] Percorso_Schede_Luci). If that field is empty, it tries a default calculated path (next to this app's own folder); if it's filled in, it uses exactly that path, whatever it is.

If you move the Schede Luci folder: press the "..." button next to "SCHEDE LUCI" and pick the SchedeLuci.pyw file again at its new location. No other file needs touching and nothing needs restarting: the field updates right away in the ini and from then on "SCHEDE LUCI" finds it there.""",
    },
}

TESTO_GUIDA_CANZONI = {
    "it": {
        "titolo": "Guida - Canzoni",
        "corpo": """COSA FA QUESTA SCHEDA
Qui gestisci l'elenco delle canzoni che gli ospiti possono votare dal telefono. Ogni volta che aggiungi, modifichi o escludi/includi una canzone, il file catalogo_canzoni.json viene riscritto da solo: e' quel file che VotoShow.py offre agli ospiti - ma SOLO come vedremo sotto in "QUANDO LE MODIFICHE HANNO EFFETTO", leggi quella parte con calma, e' la cosa piu' importante di tutta questa guida.

LE COLONNE DELLA LISTA
- Numero: solo l'ordine con cui la vedi in questa lista, non ha nessun effetto su LOR o sul voto.
- Titolo / Artista: letti da soli dal file audio quando aggiungi la canzone (se il file ha i tag giusti), oppure li scrivi tu a mano. Sono solo testo mostrato agli ospiti, non servono a nient'altro.
- Durata: solo informativa, letta dal file audio.
- Trigger: un codice tecnico tipo "Regular-01-3" che serve a VotoShow.py per capire a quale sequenza LOR corrisponde questa canzone, quando parla con l'API di LOR. Il modo piu' sicuro per compilarlo e' usare il pulsante "Carica da LOR" dentro Aggiungi/Modifica: NON scriverlo a mano a meno che tu non sappia gia' esattamente cosa stai facendo.
- Nome LOR: il nome ESATTO della sequenza cosi' come si chiama dentro LOR (Sequencer/Show Player). Deve combaciare parola per parola, altrimenti VotoShow.py non trova la sequenza giusta e la richiesta/coda non funziona per quella canzone.
- Stato: Attiva o Esclusa (vedi il pulsante Escludi/Includi piu' sotto).

I PULSANTI IN ALTO (prima riga)
- Aggiorna Lista: ricarica solo quello che vedi a schermo dal file su disco, non cambia niente.
- Aggiungi...: crea una nuova canzone votabile. Ti fa scegliere il file audio (e la copertina) con una finestra di ricerca file, prova a leggere titolo/artista da solo dal file, e ti fa compilare Trigger/Nome LOR (meglio con "Carica da LOR").
- Modifica selezionata...: stessa finestra di Aggiungi, ma sulla canzone che hai selezionato (o doppio click su una riga).
- Escludi/Includi selezionata: NON cancella mai niente dal disco. Nasconde solo quella canzone dalla pagina di voto degli ospiti (o la fa ricomparire se era esclusa) - i file audio/copertina e i dati restano intatti, e la puoi reincludere quando vuoi.

IL PULSANTE A DESTRA (stessa riga, allineato a destra apposta per tenerlo separato dai comandi sopra)
- Rigenera catalogo ora: forza subito la riscrittura di catalogo_canzoni.json dalla lista che vedi qui. Normalmente NON serve premerlo mai, perche' ogni Aggiungi/Modifica/Escludi-Includi lo fa gia' da solo in automatico - usalo solo se sospetti che qualcosa non torni.

(Nota: "Apri risultati" si trova nel tab Programmazione, accanto a "Voto" - stesso discorso, pagine del server di voto in esecuzione.)

IL PULSANTE "SCHEDE LUCI" IN ALTO (in cima alla finestra, si vede da qualsiasi scheda)
E' uno strumento completamente separato, per l'inventario fisico delle schede/canali/alimentazione delle luci - non c'entra nulla con canzoni o voto, nessun dato in comune. I dettagli completi sono nella Guida della scheda Programmazione.

QUANDO LE MODIFICHE HANNO EFFETTO - LEGGI QUESTO
VotoShow.py legge il catalogo canzoni (e tutta la sua configurazione) SOLO nel momento in cui parte, MAI mentre e' gia' in esecuzione. Quindi se lo show e' gia' acceso e tu aggiungi/modifichi/escludi una canzone qui, gli ospiti continuano a vedere la versione VECCHIA finche' non vai nella scheda Programmazione e premi "Stop ora" e poi "Avvia ora" (oppure aspetti il prossimo riavvio pianificato). Non c'e' nessun modo per far "ricaricare" il catalogo a un VotoShow.py gia' acceso senza fermarlo e farlo ripartire davvero.""",
    },
    "en": {
        "titolo": "Guide - Songs",
        "corpo": """WHAT THIS TAB DOES
Here you manage the list of songs guests can vote for from their phone. Every time you add, edit, or exclude/include a song, the catalogo_canzoni.json file gets rewritten by itself: that's the file VotoShow.py offers to guests - but ONLY as explained below in "WHEN CHANGES TAKE EFFECT", read that part carefully, it's the single most important thing in this whole guide.

THE LIST COLUMNS
- Number: just the order you see it in this list, has no effect on LOR or on voting.
- Title / Artist: read automatically from the audio file when you add the song (if the file has the right tags), or you type them by hand. Just text shown to guests, nothing else depends on them.
- Duration: informational only, read from the audio file.
- Trigger: a technical code like "Regular-01-3" that VotoShow.py uses to know which LOR sequence this song corresponds to, when talking to LOR's API. The safest way to fill it is the "Load from LOR" button inside Add/Edit: do NOT type it by hand unless you already know exactly what you're doing.
- LOR Name: the EXACT name of the sequence as it's called inside LOR (Sequencer/Show Player). It must match word for word, otherwise VotoShow.py can't find the right sequence and requests/queueing won't work for that song.
- Status: Active or Excluded (see the Exclude/Include button below).

THE BUTTONS AT THE TOP (first row)
- Refresh list: only reloads what you see on screen from the file on disk, changes nothing.
- Add...: creates a new votable song. Lets you pick the audio file (and cover) with a file browser, tries to read title/artist from the file on its own, and lets you fill in Trigger/LOR Name (best done with "Load from LOR").
- Edit selected...: same window as Add, but for the song you selected (or double-click a row).
- Exclude/Include selected: NEVER deletes anything from disk. Only hides that song from the guests' voting page (or brings it back if it was excluded) - audio/cover files and data stay untouched, and you can re-include it any time.

THE BUTTON ON THE RIGHT (same row, aligned right on purpose to keep it separate from the commands above)
- Rebuild catalog now: forces catalogo_canzoni.json to be rewritten right now from the list you see here. Normally you never need to press this, because every Add/Edit/Exclude-Include already does it automatically - only use it if you suspect something is out of sync.

(Note: "Open results" is in the Scheduling tab, next to "Vote" - same idea, pages served by the running voting server.)

THE "SCHEDE LUCI" BUTTON AT THE TOP (top of the window, visible from any tab)
This is a completely separate tool, for the physical inventory of light boards/channels/power supplies - it has nothing to do with songs or voting, no shared data. Full details are in the Scheduling tab's Guide.

WHEN CHANGES TAKE EFFECT - READ THIS
VotoShow.py reads the song catalog (and its whole configuration) ONLY the moment it starts, NEVER while it's already running. So if the show is already on and you add/edit/exclude a song here, guests keep seeing the OLD version until you go to the Scheduling tab and press "Stop now" then "Start now" (or wait for the next scheduled restart). There is no way to make an already-running VotoShow.py "reload" the catalog without actually stopping and restarting it.""",
    },
}


TESTO_GUIDA_PULIZIA = {
    "it": {
        "titolo": "Guida - Pulizia automatica",
        "corpo": """LA PULIZIA E' AUTOMATICA, NON MANUALE

Questo box configura cosa succede da solo ogni giorno, un tot di minuti PRIMA dell'Orario Accensione - non serve premere nulla ogni volta. Una volta premuto "Pianifica Win Auto" nel box Stato Pianificazione, Windows esegue la pulizia da solo, tutti i giorni schedulati, senza che tu debba ricordartene.

Il pulsante "Pulisci ora" NON e' quello automatico: serve solo per provare subito la pulizia (test) o per farla su richiesta in un momento qualsiasi - va premuto a mano, non parte mai da solo in base a orario/giorni.

COSA PULISCE
- Chiude Chrome, se la casella "Chiudi Chrome" e' spuntata (libera RAM).
- Svuota i file temporanei di Windows e SOLO la cache di Chrome, se "Svuota file temporanei e cache di Chrome" e' spuntata (mai password/preferiti/cronologia).
- Chiude anche i "Programmi da chiudere in automatico" che aggiungi tu col pulsante "Aggiungi...": si apre una ricerca file per scegliere l'eseguibile esatto (anche su un altro disco), non serve scrivere nulla a mano.

CONFERMA PRIMA DI CHIUDERE
Prima di chiudere Chrome o un qualsiasi programma della lista, compare sempre un popup di conferma con Si'/No, cosi' non si chiude niente all'improvviso se qualcuno lo sta usando. Il popup ha un conto alla rovescia di 20 secondi: se nessuno risponde (es. pulizia notturna con nessuno davanti al PC), di default NON lo chiude - non blocca la pulizia in eterno, ma nemmeno forza la chiusura senza permesso.

AGGIORNAMENTI WINDOWS - IMPORTANTE
La casella "Blocca aggiornamenti Windows" prova a fermare Windows Update in automatico ogni volta che parte la pulizia, cosi' non scatta un riavvio o un'installazione a sorpresa mentre lo show e' in corso. Perche' funzioni davvero, i 3 task pianificati devono essere creati con i privilegi piu' alti disponibili - e QUESTO RICHIEDE CHE MANAGERSHOW.PYW SIA GIA' AVVIATO CON "ESEGUI COME AMMINISTRATORE" NEL MOMENTO STESSO IN CUI PREMI "PIANIFICA WIN AUTO" (ManagerShow ora prova a rilanciarsi da solo elevato ad ogni avvio, chiedendo conferma con il prompt di Windows). Non basta che l'account sia amministratore: se ManagerShow non e' elevato in quel momento, Windows nega la creazione dei task con quei privilegi (errore "Accesso negato"), quindi i task vengono creati comunque ma con permessi normali, e il blocco automatico semplicemente non fara' nulla - non rompe la pianificazione, ma nemmeno protegge. Se manca questo requisito, un avviso automatico te lo segnala subito dopo aver premuto il pulsante.

QUINDI, PER SICUREZZA: SE NON SEI SICURO DI AVER AVVIATO MANAGERSHOW COME AMMINISTRATORE, VAI SEMPRE A CONTROLLARE E BLOCCARE GLI AGGIORNAMENTI DI WINDOWS A MANO (Impostazioni > Windows Update > Sospendi aggiornamenti), ALMENO 2-3 ORE PRIMA DELL'ORARIO DI ACCENSIONE DELLO SHOW, COSI' NON AUMENTA IL RISCHIO DI CRASH DI LOR E DEL RESTO DEL SISTEMA DURANTE LA SERATA.""",
    },
    "en": {
        "titolo": "Guide - Automatic cleanup",
        "corpo": """CLEANUP IS AUTOMATIC, NOT MANUAL

This box configures what happens by itself every day, a number of minutes BEFORE Start time - you don't need to press anything each time. Once you've pressed "Schedule Win Auto" in the Schedule Status box, Windows runs the cleanup on its own, every scheduled day, without you having to remember it.

The "Clean now" button is NOT the automatic one: it's only for testing the cleanup right away, or running it on demand at any moment - it must be pressed by hand, it never starts on its own based on time/days.

WHAT IT CLEANS
- Closes Chrome, if the "Close Chrome" box is checked (frees RAM).
- Clears Windows temp files and ONLY Chrome's cache, if "Clear temp files and Chrome cache" is checked (never passwords/bookmarks/history).
- Also closes the "Programs to close automatically" you add with the "Add..." button: it opens a file search to pick the exact program (even on another drive), no need to type anything by hand.

CONFIRMATION BEFORE CLOSING
Before closing Chrome or any program on the list, a Yes/No confirmation popup always appears, so nothing closes unexpectedly if someone is using it. The popup has a 20-second countdown: if nobody answers (e.g. an overnight cleanup with nobody at the PC), by default it does NOT close it - it won't block cleanup forever, but it won't force a close without permission either.

WINDOWS UPDATES - IMPORTANT
The "Block Windows updates" checkbox tries to stop Windows Update automatically every time cleanup runs, so a surprise reboot or install doesn't happen while the show is on. For this to actually work, the 3 scheduled tasks need to be created with the highest privileges available - and THIS REQUIRES MANAGERSHOW.PYW TO ALREADY BE RUNNING WITH "RUN AS ADMINISTRATOR" AT THE EXACT MOMENT YOU PRESS "SCHEDULE WIN AUTO" (ManagerShow now tries to relaunch itself elevated on every startup, asking for confirmation via the Windows prompt). Being an administrator account isn't enough: if ManagerShow isn't elevated right then, Windows denies creating the tasks with those privileges ("Access denied" error), so the tasks still get created but with normal permissions, and the automatic block simply won't do anything - it won't break the scheduling, but it won't protect you either. If this requirement is missing, an automatic warning tells you right after you press the button.

SO, TO BE SAFE: IF YOU'RE NOT SURE YOU STARTED MANAGERSHOW AS ADMINISTRATOR, ALWAYS GO AND BLOCK WINDOWS UPDATES BY HAND (Settings > Windows Update > Pause updates), AT LEAST 2-3 HOURS BEFORE THE SHOW'S START TIME, SO THE RISK OF LOR AND THE REST OF THE SYSTEM CRASHING DURING THE EVENING DOESN'T GO UP.""",
    },
}


class FinestraGuida(tk.Toplevel):
    def __init__(self, padre, lingua: str, testo_guida: dict = None):
        super().__init__(padre)
        testo_guida = testo_guida if testo_guida is not None else TESTO_GUIDA
        self.title(testo_guida[lingua]["titolo"])
        self.geometry("480x640")
        self.transient(padre)

        testo_widget = tk.Text(self, wrap="word", padx=12, pady=12, font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=testo_widget.yview)
        testo_widget.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        testo_widget.pack(side="left", fill="both", expand=True)

        testo_widget.insert("1.0", testo_guida[lingua]["corpo"])
        testo_widget.configure(state="disabled")


class InfoAlPassaggioMouse:
    """Piccolo tooltip a comparsa quando il mouse si ferma su un widget
    (nessuna libreria esterna, solo tkinter): usato per pulsanti la cui
    unica etichetta (es. "...") non basta a spiegare cosa fanno."""

    def __init__(self, widget):
        self.widget = widget
        self.testo = ""
        self.finestra = None
        widget.bind("<Enter>", self._mostra)
        widget.bind("<Leave>", self._nascondi)

    def imposta_testo(self, testo: str) -> None:
        self.testo = testo

    def _mostra(self, evento=None):
        if self.finestra is not None or not self.testo:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.finestra = tk.Toplevel(self.widget)
        self.finestra.wm_overrideredirect(True)
        self.finestra.wm_geometry(f"+{x}+{y}")
        ttk.Label(
            self.finestra, text=self.testo, background="#ffffe0",
            relief="solid", borderwidth=1, padding=(6, 3), wraplength=260,
        ).pack()

    def _nascondi(self, evento=None):
        if self.finestra is not None:
            self.finestra.destroy()
            self.finestra = None


# ----------------------------------------------------------------------
# Finestra principale
# ----------------------------------------------------------------------
class ManagerShow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.lingua = leggi_lingua_iniziale()
        percorso_icona = Path(__file__).resolve().parent / "ManagerShow.ico"
        if percorso_icona.is_file():
            self.iconbitmap(str(percorso_icona))
        self.geometry("820x1040+340+5")
        self.finestra_guida = None
        self.finestra_guida_pulizia = None
        self.finestra_guida_canzoni = None
        self.bind("<Configure>", self._al_muovere_finestra)

        barra_superiore = ttk.Frame(self)
        barra_superiore.pack(fill="x", padx=8, pady=(8, 0))
        # tk.Button invece di ttk.Button: su Windows il tema nativo ("vista")
        # ignora bg/fg sui pulsanti ttk, con tk.Button il colore si vede sempre
        self.bottone_lingua = tk.Button(
            barra_superiore, command=self._cambia_lingua, width=4,
            bg="#1565c0", fg="white", activebackground="#0d47a1", activeforeground="white",
            relief="raised", cursor="hand2",
        )
        self.bottone_lingua.pack(side="right")

        # Schede Luci (2026-07-24): tool separato per l'inventario tecnico
        # schede/canali/alimentazione, vive fuori da questa cartella -
        # qui solo un lancio, nessuno stato condiviso. Il pulsante "..."
        # permette di indicare dove si trova se la cartella viene spostata,
        # senza dover piu' modificare il codice.
        self.bottone_percorso_schede_luci = ttk.Button(
            barra_superiore, text="...", width=3, command=cambia_percorso_schede_luci
        )
        self.bottone_percorso_schede_luci.pack(side="right", padx=(0, 6))
        self.tooltip_percorso_schede_luci = InfoAlPassaggioMouse(self.bottone_percorso_schede_luci)
        ttk.Button(barra_superiore, text="SCHEDE LUCI", command=apri_schede_luci).pack(
            side="right", padx=(0, 2)
        )

        self.bottone_salva_ordine_schede = ttk.Button(
            barra_superiore, command=self._salva_ordine_schede,
        )
        self.bottone_salva_ordine_schede.pack(side="left")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.tab_canzoni = ttk.Frame(self.notebook)
        self.tab_programmazione = ttk.Frame(self.notebook)
        self.tab_saluto_babbonatale = ttk.Frame(self.notebook)
        self.tab_benvenuto = ttk.Frame(self.notebook)
        self.tab_info = ttk.Frame(self.notebook)
        self.tab_qrcode = ttk.Frame(self.notebook)
        self.tab_storico = ttk.Frame(self.notebook)
        self.tab_grafico = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_canzoni, text="Canzoni")
        self.notebook.add(self.tab_programmazione, text="Programmazione")
        self.notebook.add(self.tab_benvenuto, text="Benvenuto")
        self.notebook.add(self.tab_info, text="Info")
        self.notebook.add(self.tab_qrcode, text="QR Code")
        self.notebook.add(self.tab_saluto_babbonatale, text="Saluto Babbo Natale")
        self.notebook.add(self.tab_storico, text="Storico")
        self.notebook.add(self.tab_grafico, text="Grafico")

        self._costruisci_tab_canzoni(self.tab_canzoni)
        self._costruisci_tab_programmazione(self.tab_programmazione)
        self._costruisci_tab_benvenuto(self.tab_benvenuto)
        self._costruisci_tab_info(self.tab_info)
        self._costruisci_tab_qrcode(self.tab_qrcode)
        self._costruisci_tab_saluto_babbonatale(self.tab_saluto_babbonatale)
        self._costruisci_tab_storico(self.tab_storico)
        self._costruisci_tab_grafico(self.tab_grafico)

        self._chiave_per_scheda = {
            self.tab_canzoni: "canzoni",
            self.tab_programmazione: "programmazione",
            self.tab_benvenuto: "benvenuto",
            self.tab_info: "info",
            self.tab_qrcode: "qrcode",
            self.tab_saluto_babbonatale: "saluto_babbonatale",
            self.tab_storico: "storico",
            self.tab_grafico: "grafico",
        }
        self._scheda_per_chiave = {chiave: scheda for scheda, chiave in self._chiave_per_scheda.items()}
        self._chiave_per_percorso_scheda = {str(scheda): chiave for scheda, chiave in self._chiave_per_scheda.items()}

        self._carica_ordine_schede_da_ini()
        self._abilita_trascinamento_schede()

        self._ritraduci_interfaccia()
        self.ricarica_lista()
        self._aggiorna_stato_pianificazione()
        self._aggiorna_stato_processi()
        self._pianifica_aggiornamento_storico()

    def _t(self, chiave: str, **valori) -> str:
        return testo(self.lingua, chiave, **valori)

    # ------------------------------------------------------------------
    # Scheda Canzoni
    # ------------------------------------------------------------------
    def _costruisci_tab_canzoni(self, padre):
        frame_guida_bottone_canzoni = ttk.Frame(padre)
        frame_guida_bottone_canzoni.pack(fill="x", padx=8, pady=(8, 0), anchor="e")
        self.bottone_guida_canzoni = ttk.Button(frame_guida_bottone_canzoni, command=self._apri_guida_canzoni)
        self.bottone_guida_canzoni.pack(side="right")

        colonne = ("numero", "titolo", "artista", "durata", "trigger", "nome_lor", "stato")
        self.albero = ttk.Treeview(padre, columns=colonne, show="headings", selectmode="browse")
        for colonna, larghezza in (
            ("numero", 36), ("titolo", 220), ("artista", 160), ("durata", 70), ("trigger", 130), ("nome_lor", 180), ("stato", 110),
        ):
            self.albero.column(colonna, width=larghezza, stretch=(colonna != "numero"), anchor="center" if colonna == "numero" else "w")
        self.albero.pack(fill="both", expand=True, padx=8, pady=8)
        self.albero.bind("<Double-1>", lambda evento: self._modifica())

        frame_bottoni_1 = ttk.Frame(padre)
        frame_bottoni_1.pack(fill="x", padx=8, pady=(0, 8))

        self.bottone_aggiorna = ttk.Button(frame_bottoni_1, command=self.ricarica_lista)
        self.bottone_aggiorna.pack(side="left", padx=4)
        self.bottone_aggiungi = ttk.Button(frame_bottoni_1, command=self._aggiungi)
        self.bottone_aggiungi.pack(side="left", padx=4)
        self.bottone_modifica = ttk.Button(frame_bottoni_1, command=self._modifica)
        self.bottone_modifica.pack(side="left", padx=4)
        self.bottone_escludi = ttk.Button(frame_bottoni_1, command=self._alterna_esclusione)
        self.bottone_escludi.pack(side="left", padx=4)

        # Rigenera catalogo: allineato a destra sulla stessa riga, per
        # tenerlo distinto dai comandi di modifica lista. "Apri risultati"
        # sta invece nel tab Programmazione, accanto a "Voto" (stesso
        # discorso: pagine del server di voto in esecuzione).
        self.bottone_rigenera = ttk.Button(frame_bottoni_1, command=self._rigenera_manuale)
        self.bottone_rigenera.pack(side="right", padx=4)

    def _carica_ordine_schede_da_ini(self):
        parser = carica_parser()
        if SEZIONE_INTERFACCIA not in parser:
            return
        ordine_testo = parser.get(SEZIONE_INTERFACCIA, "Ordine_Schede", fallback="").strip()
        if not ordine_testo:
            return
        chiavi_salvate = [c.strip() for c in ordine_testo.split(",") if c.strip()]
        for indice, chiave in enumerate(chiavi_salvate):
            scheda = self._scheda_per_chiave.get(chiave)
            if scheda is not None:
                self.notebook.insert(indice, scheda)

    def _salva_ordine_schede(self):
        chiavi_in_ordine = [
            self._chiave_per_percorso_scheda[percorso]
            for percorso in self.notebook.tabs()
            if percorso in self._chiave_per_percorso_scheda
        ]
        parser = carica_parser()
        if SEZIONE_INTERFACCIA not in parser:
            parser[SEZIONE_INTERFACCIA] = {}
        parser[SEZIONE_INTERFACCIA]["Ordine_Schede"] = ",".join(chiavi_in_ordine)
        salva_parser(parser)
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_ordine_schede_salvato_testo"))

    def _abilita_trascinamento_schede(self):
        """Permette di riordinare le schede trascinandole con il mouse
        (come i tab di un browser). Il nuovo ordine resta attivo solo
        per questa sessione finche' non si preme "Salva ordine schede":
        solo allora viene scritto nell'ini e ripristinato ai prossimi
        avvii."""
        self._scheda_in_trascinamento = None
        self.notebook.bind("<ButtonPress-1>", self._inizio_trascinamento_scheda)
        self.notebook.bind("<B1-Motion>", self._durante_trascinamento_scheda)
        self.notebook.bind("<ButtonRelease-1>", self._fine_trascinamento_scheda)

    def _indice_scheda_sotto_mouse(self, evento):
        try:
            return self.notebook.index(f"@{evento.x},{evento.y}")
        except tk.TclError:
            return None

    def _inizio_trascinamento_scheda(self, evento):
        self._scheda_in_trascinamento = self._indice_scheda_sotto_mouse(evento)

    def _durante_trascinamento_scheda(self, evento):
        if self._scheda_in_trascinamento is None:
            return
        indice_sotto_mouse = self._indice_scheda_sotto_mouse(evento)
        if indice_sotto_mouse is None or indice_sotto_mouse == self._scheda_in_trascinamento:
            return
        scheda = self.notebook.tabs()[self._scheda_in_trascinamento]
        self.notebook.insert(indice_sotto_mouse, scheda)
        self._scheda_in_trascinamento = indice_sotto_mouse

    def _fine_trascinamento_scheda(self, evento):
        self._scheda_in_trascinamento = None

    def _cambia_lingua(self):
        self.lingua = "en" if self.lingua == "it" else "it"
        self._ritraduci_interfaccia()
        self.ricarica_lista()

    def _ritraduci_interfaccia(self):
        self.title(self._t("ms_titolo_finestra"))
        self.notebook.tab(self.tab_canzoni, text=self._t("ms_scheda_canzoni"))
        self.notebook.tab(self.tab_programmazione, text=self._t("ms_scheda_programmazione"))
        self.notebook.tab(self.tab_benvenuto, text=self._t("ms_scheda_benvenuto"))
        self.notebook.tab(self.tab_info, text=self._t("ms_scheda_info"))
        self.notebook.tab(self.tab_qrcode, text=self._t("ms_scheda_qrcode"))
        self.notebook.tab(self.tab_saluto_babbonatale, text=self._t("ms_scheda_saluto_babbonatale"))
        self.notebook.tab(self.tab_storico, text=self._t("ms_scheda_storico"))
        self.notebook.tab(self.tab_grafico, text=self._t("ms_scheda_grafico"))
        self.bottone_lingua.configure(text=self._t("gc_lingua_bottone"))
        self.bottone_salva_ordine_schede.configure(text=self._t("ms_bottone_salva_ordine_schede"))
        self.tooltip_percorso_schede_luci.imposta_testo(self._t("ms_tooltip_percorso_schede_luci"))

        self.albero.heading("numero", text=self._t("gc_colonna_numero"))
        self.albero.heading("titolo", text=self._t("gc_colonna_titolo"))
        self.albero.heading("artista", text=self._t("gc_colonna_artista"))
        self.albero.heading("durata", text=self._t("gc_colonna_durata"))
        self.albero.heading("trigger", text=self._t("gc_colonna_trigger"))
        self.albero.heading("nome_lor", text=self._t("gc_colonna_nome_lor"))
        self.albero.heading("stato", text=self._t("gc_colonna_stato"))

        self.bottone_aggiorna.configure(text=self._t("gc_bottone_aggiorna_lista"))
        self.bottone_aggiungi.configure(text=self._t("gc_bottone_aggiungi"))
        self.bottone_modifica.configure(text=self._t("gc_bottone_modifica"))
        self.bottone_escludi.configure(text=self._t("gc_bottone_escludi_includi"))
        self.bottone_apri_voto.configure(text=self._t("gc_bottone_apri_voto"))
        self.bottone_apri_risultati.configure(text=self._t("gc_bottone_apri_risultati"))
        self.bottone_apri_benvenuto.configure(text=self._t("gc_bottone_apri_benvenuto"))
        self.bottone_test_multi_voto.configure(text=self._t("gc_bottone_test_multi_voto"))
        self.bottone_rigenera.configure(text=self._t("gc_bottone_rigenera"))

        self.frame_stato_live.configure(text=self._t("ms_titolo_stato_attuale"))
        self.frame_orari.configure(text=self._t("ms_titolo_orari"))
        self.label_accensione.configure(text=self._t("ms_campo_accensione"))
        self.label_spegnimento.configure(text=self._t("ms_campo_spegnimento"))
        self.label_giorni.configure(text=self._t("ms_titolo_giorni"))
        for indice, checkbutton in enumerate(self.checkbutton_giorni):
            checkbutton.configure(text=self._t(f"ms_giorno_{indice}"))
        self.frame_pulizia.configure(text=self._t("ms_titolo_pulizia"))
        self.label_minuti_anticipo.configure(text=self._t("ms_campo_minuti_anticipo"))
        self.bottone_pulisci_ora.configure(text=self._t("ms_bottone_pulisci_ora"))
        self.bottone_guida_pulizia.configure(text=self._t("ms_bottone_guida_pulizia"))
        self.checkbutton_chiudi_chrome.configure(text=self._t("ms_titolo_chiudi_chrome"))
        self.checkbutton_pulisci_cache.configure(text=self._t("ms_titolo_pulisci_cache"))
        self.checkbutton_blocca_aggiornamenti.configure(text=self._t("ms_titolo_blocca_aggiornamenti"))
        self.label_programmi_extra.configure(text=self._t("ms_label_programmi_extra"))
        self.bottone_aggiungi_programma.configure(text=self._t("ms_bottone_apri_gestione_programmi"))
        self.bottone_salva_orari.configure(text=self._t("ms_bottone_salva_orari"))

        self.frame_modalita_coda.configure(text=self._t("ms_titolo_modalita_coda"))
        self.label_modalita_coda.configure(text=self._t("ms_modalita_coda_label"))
        chiave_selezionata = self._chiave_da_etichetta_modalita_coda(self.var_modalita_coda_display.get())
        self.combo_modalita_coda.configure(values=self._etichette_modalita_coda())
        self.var_modalita_coda_display.set(self._t(self.MODALITA_CODA_CHIAVE_TRADUZIONE[chiave_selezionata]))
        self.label_tetto_coda.configure(text=self._t("ms_tetto_coda_label"))

        self.label_raffreddamento.configure(text=self._t("ms_raffreddamento_label"))
        chiave_raffreddamento_selezionata = self._chiave_da_etichetta_raffreddamento_tipo(
            self.var_raffreddamento_tipo_display.get()
        )
        self.combo_raffreddamento_tipo.configure(values=self._etichette_raffreddamento_tipo())
        self.var_raffreddamento_tipo_display.set(
            self._t(self.RAFFREDDAMENTO_TIPO_CHIAVE_TRADUZIONE[chiave_raffreddamento_selezionata])
        )
        self.label_raffreddamento_valore.configure(text=self._t("ms_raffreddamento_valore_label"))

        self.label_nota_modalita_coda.configure(text=self._t(self.MODALITA_CODA_CHIAVE_NOTA[chiave_selezionata]))
        self.bottone_salva_modalita_coda.configure(text=self._t("ms_bottone_salva_modalita_coda"))

        self.frame_saluto_babbonatale.configure(text=self._t("ms_titolo_saluto_babbonatale"))
        self.checkbutton_saluto_babbonatale.configure(text=self._t("ms_checkbox_saluto_babbonatale"))
        self.label_nota_saluto_babbonatale.configure(text=self._t("ms_nota_saluto_babbonatale"))

        self.frame_token_admin.configure(text=self._t("ms_titolo_token_admin"))
        self.label_token_admin.configure(text=self._t("ms_label_token_admin"))
        self.bottone_mostra_token_admin.configure(
            text=self._t("ms_bottone_nascondi_token" if self.token_admin_visibile else "ms_bottone_mostra_token")
        )
        self.bottone_genera_token_admin.configure(text=self._t("ms_bottone_genera_token"))
        self.label_nota_token_admin.configure(text=self._t("ms_nota_token_admin"))
        self.bottone_salva_token_admin.configure(text=self._t("ms_bottone_salva_token"))
        self.bottone_salva_saluto_babbonatale.configure(text=self._t("ms_bottone_salva_saluto_babbonatale"))

        self.bottone_guida.configure(text=self._t("ms_bottone_guida"))
        self.bottone_guida_canzoni.configure(text=self._t("ms_bottone_guida"))
        self.bottone_guida_saluto_babbonatale.configure(text=self._t("ms_bottone_guida"))
        self.label_avviso_avvia_ferma.configure(text=self._t("ms_avviso_avvia_ferma_testo"))
        if self.finestra_guida is not None and self.finestra_guida.winfo_exists():
            self.finestra_guida.destroy()
            self.finestra_guida = None
        if self.finestra_guida_pulizia is not None and self.finestra_guida_pulizia.winfo_exists():
            self.finestra_guida_pulizia.destroy()
            self.finestra_guida_pulizia = None
        if self.finestra_guida_canzoni is not None and self.finestra_guida_canzoni.winfo_exists():
            self.finestra_guida_canzoni.destroy()
            self.finestra_guida_canzoni = None

        self._aggiorna_stato_pianificazione()
        self._ritraduci_tab_benvenuto()
        self._ritraduci_tab_info()
        self._ritraduci_tab_qrcode()
        self._ritraduci_tab_storico()
        self._ritraduci_tab_grafico()

    def _ritraduci_tab_qrcode(self):
        self.label_nota_qr.configure(text=self._t("ms_qr_nota_scheda"))
        self.frame_qr_wifi.configure(text=self._t("ms_qr_titolo_wifi"))
        self.label_qr_campo_ssid.configure(text=self._t("ms_qr_campo_ssid"))
        self.label_qr_campo_password.configure(text=self._t("ms_qr_campo_password"))
        self.label_qr_campo_sicurezza.configure(text=self._t("ms_qr_campo_sicurezza"))
        chiave_sicurezza = self._chiave_da_etichetta_qr_sicurezza(self.var_qr_sicurezza_display.get())
        self.combo_qr_sicurezza.configure(values=self._etichette_qr_sicurezza())
        self.var_qr_sicurezza_display.set(self._t(self.QR_SICUREZZA_CHIAVE_TRADUZIONE[chiave_sicurezza]))
        self.frame_qr_indirizzo.configure(text=self._t("ms_qr_titolo_indirizzo"))
        self.label_qr_campo_ip.configure(text=self._t("ms_qr_campo_ip"))
        self.bottone_qr_rileva_ip.configure(text=self._t("ms_qr_bottone_rileva_ip"))
        self.label_nota_qr_ip.configure(text=self._t("ms_qr_nota_ip"))
        self.bottone_qr_genera.configure(text=self._t("ms_qr_bottone_genera"))
        self.label_qr_etichetta_wifi.configure(text=self._t("ms_qr_etichetta_wifi"))
        self.label_qr_etichetta_voto.configure(text=self._t("ms_qr_etichetta_voto"))
        self.bottone_qr_salva_wifi.configure(text=self._t("ms_qr_bottone_salva_wifi"))
        self.bottone_qr_salva_voto.configure(text=self._t("ms_qr_bottone_salva_voto"))

        self.frame_qr_cartello.configure(text=self._t("ms_qr_titolo_cartello"))
        self.label_qr_cartello_titolo.configure(text=self._t("ms_qr_campo_cartello_titolo"))
        self.label_qr_cartello_sottotitolo.configure(text=self._t("ms_qr_campo_cartello_sottotitolo"))
        self.checkbutton_qr_cartello_includi_voto.configure(text=self._t("ms_qr_checkbox_cartello_includi_voto"))
        self.bottone_qr_genera_cartello.configure(text=self._t("ms_qr_bottone_genera_cartello"))
        self.bottone_qr_stampa_cartello.configure(text=self._t("ms_qr_bottone_stampa_cartello"))

        self.frame_qr_profili.configure(text=self._t("ms_qr_titolo_profili"))
        self.bottone_qr_salva_profilo.configure(text=self._t("ms_qr_bottone_salva_profilo"))
        self.albero_qr_profili.heading("nome", text=self._t("ms_qr_colonna_nome"))
        self.albero_qr_profili.heading("ssid", text=self._t("ms_qr_colonna_ssid"))
        self.albero_qr_profili.heading("ip", text=self._t("ms_qr_colonna_ip"))
        self.bottone_qr_carica_profilo.configure(text=self._t("ms_qr_bottone_carica_profilo"))
        self.bottone_qr_elimina_profilo.configure(text=self._t("ms_qr_bottone_elimina_profilo"))

    def _ritraduci_tab_info(self):
        self.label_nota_info.configure(text=self._t("ms_inf_nota_scheda"))
        self.frame_visibilita_info.configure(text=self._t("ms_inf_titolo_visibilita"))
        self.checkbutton_visibile_info.configure(text=self._t("ms_inf_checkbox_visibile"))
        self.label_nota_visibilita_info.configure(text=self._t("ms_inf_nota_visibilita"))
        self.frame_testo_info.configure(text=self._t("ms_inf_titolo_testo"))
        self.label_nota_testo_info.configure(text=self._t("ms_inf_nota_testo"))
        self.frame_immagine_info.configure(text=self._t("ms_inf_titolo_immagine"))
        self.label_campo_immagine_info.configure(text=self._t("ms_inf_campo_immagine"))
        self.bottone_sfoglia_immagine_info.configure(text=self._t("ms_inf_bottone_sfoglia_immagine"))
        self.bottone_rimuovi_immagine_info.configure(text=self._t("ms_inf_bottone_rimuovi_immagine"))
        self.frame_audio_info.configure(text=self._t("ms_inf_titolo_audio"))
        self.label_campo_audio_info.configure(text=self._t("ms_inf_campo_audio"))
        self.bottone_sfoglia_audio_info.configure(text=self._t("ms_inf_bottone_sfoglia_audio"))
        self.bottone_rimuovi_audio_info.configure(text=self._t("ms_inf_bottone_rimuovi_audio"))
        self.frame_colore_info.configure(text=self._t("ms_inf_titolo_colore"))
        self.bottone_cambia_colore_info.configure(text=self._t("ms_inf_bottone_cambia_colore"))
        self.label_nota_colore_info.configure(text=self._t("ms_inf_nota_colore"))
        self.label_campo_link_donazione.configure(text=self._t("ms_inf_campo_link_donazione"))
        self.frame_donazione_info.configure(text=self._t("ms_inf_titolo_donazione"))
        self.label_nota_donazione_info.configure(text=self._t("ms_inf_nota_donazione"))
        self.frame_grazie_info.configure(text=self._t("ms_inf_titolo_grazie"))
        self.label_nota_grazie_info.configure(text=self._t("ms_inf_nota_grazie"))
        self.bottone_anteprima_grazie_info.configure(text=self._t("ms_inf_bottone_anteprima_grazie"))
        self.bottone_salva_info.configure(text=self._t("ms_inf_bottone_salva"))
        self.bottone_anteprima_info.configure(text=self._t("ms_inf_bottone_anteprima"))
        if self.var_nome_immagine_info.get() in ("", self._t_altra_lingua("ms_bv_nessuna_scelta")):
            self.var_nome_immagine_info.set(self._t("ms_bv_nessuna_scelta"))
        if self.var_nome_audio_info.get() in ("", self._t_altra_lingua("ms_bv_nessuna_scelta")):
            self.var_nome_audio_info.set(self._t("ms_bv_nessuna_scelta"))

    def _ritraduci_tab_benvenuto(self):
        self.label_nota_benvenuto.configure(text=self._t("ms_bv_nota_scheda"))
        self.frame_testo_benvenuto.configure(text=self._t("ms_bv_titolo_testo"))
        self.label_nota_testo_benvenuto.configure(text=self._t("ms_bv_nota_testo"))
        self.frame_immagine_benvenuto.configure(text=self._t("ms_bv_titolo_immagine"))
        self.label_campo_immagine_benvenuto.configure(text=self._t("ms_bv_campo_immagine"))
        self.bottone_sfoglia_immagine_benvenuto.configure(text=self._t("ms_bv_bottone_sfoglia_immagine"))
        self.bottone_rimuovi_immagine_benvenuto.configure(text=self._t("ms_bv_bottone_rimuovi_immagine"))
        self.frame_audio_benvenuto.configure(text=self._t("ms_bv_titolo_audio"))
        self.label_campo_audio_benvenuto.configure(text=self._t("ms_bv_campo_audio"))
        self.bottone_sfoglia_audio_benvenuto.configure(text=self._t("ms_bv_bottone_sfoglia_audio"))
        self.bottone_rimuovi_audio_benvenuto.configure(text=self._t("ms_bv_bottone_rimuovi_audio"))
        self.frame_colore_benvenuto.configure(text=self._t("ms_bv_titolo_colore"))
        self.bottone_cambia_colore_benvenuto.configure(text=self._t("ms_bv_bottone_cambia_colore"))
        self.label_nota_colore_benvenuto.configure(text=self._t("ms_bv_nota_colore"))
        self.bottone_salva_benvenuto.configure(text=self._t("ms_bv_bottone_salva"))
        self.bottone_anteprima_benvenuto.configure(text=self._t("ms_bv_bottone_anteprima"))
        if self.var_nome_immagine_benvenuto.get() in ("", self._t_altra_lingua("ms_bv_nessuna_scelta")):
            self.var_nome_immagine_benvenuto.set(self._t("ms_bv_nessuna_scelta"))
        if self.var_nome_audio_benvenuto.get() in ("", self._t_altra_lingua("ms_bv_nessuna_scelta")):
            self.var_nome_audio_benvenuto.set(self._t("ms_bv_nessuna_scelta"))

    def _t_altra_lingua(self, chiave: str) -> str:
        """Traduzione nell'altra lingua (serve solo per riconoscere un
        placeholder tipo '(nessuno scelto)' dopo un cambio lingua, senza
        doverlo tracciare con uno stato a parte)."""
        altra = "en" if self.lingua == "it" else "it"
        return testo(altra, chiave)

    # ------------------------------------------------------------------
    # Finestra Guida: agganciata alla GUI principale, si riposiziona
    # sempre di fianco (segue lo spostamento, e riappare vicina anche
    # se era stata chiusa e la finestra principale spostata nel frattempo).
    # ------------------------------------------------------------------
    def _al_muovere_finestra(self, evento):
        if evento.widget is not self:
            return
        self._posiziona_guida_se_aperta()
        self._posiziona_guida_pulizia_se_aperta()
        self._posiziona_guida_canzoni_se_aperta()

    def _posiziona_guida_se_aperta(self):
        if self.finestra_guida is None or not self.finestra_guida.winfo_exists():
            return
        x, y = self._calcola_posizione_guida(self.finestra_guida)
        self.finestra_guida.geometry(f"+{x}+{y}")

    def _posiziona_guida_canzoni_se_aperta(self):
        if self.finestra_guida_canzoni is None or not self.finestra_guida_canzoni.winfo_exists():
            return
        x, y = self._calcola_posizione_guida(self.finestra_guida_canzoni)
        self.finestra_guida_canzoni.geometry(f"+{x}+{y}")

    def _calcola_posizione_guida(self, finestra_guida=None):
        self.update_idletasks()
        x_principale = self.winfo_x()
        y_principale = self.winfo_y()
        larghezza_principale = self.winfo_width()
        larghezza_guida = finestra_guida.winfo_width() if finestra_guida is not None else 480
        schermo_larghezza = self.winfo_screenwidth()

        x = x_principale + larghezza_principale + 8
        if x + larghezza_guida > schermo_larghezza:
            x = max(x_principale - larghezza_guida - 8, 0)
        return x, y_principale

    def _apri_guida(self):
        if self.finestra_guida is not None and self.finestra_guida.winfo_exists():
            self.finestra_guida.lift()
            self.finestra_guida.focus_set()
            return

        self.finestra_guida = FinestraGuida(self, self.lingua)
        self.finestra_guida.protocol("WM_DELETE_WINDOW", self._chiudi_guida)
        self.finestra_guida.update_idletasks()
        x, y = self._calcola_posizione_guida(self.finestra_guida)
        self.finestra_guida.geometry(f"+{x}+{y}")

    def _chiudi_guida(self):
        if self.finestra_guida is not None:
            self.finestra_guida.destroy()
        self.finestra_guida = None

    def _apri_guida_canzoni(self):
        if self.finestra_guida_canzoni is not None and self.finestra_guida_canzoni.winfo_exists():
            self.finestra_guida_canzoni.lift()
            self.finestra_guida_canzoni.focus_set()
            return

        self.finestra_guida_canzoni = FinestraGuida(self, self.lingua, TESTO_GUIDA_CANZONI)
        self.finestra_guida_canzoni.protocol("WM_DELETE_WINDOW", self._chiudi_guida_canzoni)
        self.finestra_guida_canzoni.update_idletasks()
        x, y = self._calcola_posizione_guida(self.finestra_guida_canzoni)
        self.finestra_guida_canzoni.geometry(f"+{x}+{y}")

    def _chiudi_guida_canzoni(self):
        if self.finestra_guida_canzoni is not None:
            self.finestra_guida_canzoni.destroy()
        self.finestra_guida_canzoni = None

    # ------------------------------------------------------------------
    # Finestra Guida Pulizia: stesso principio della Guida principale, ma
    # agganciata SOTTO ManagerShow, allineata all'angolo sinistro e larga
    # quanto ManagerShow (angolo destro coincidente).
    # ------------------------------------------------------------------
    def _posiziona_guida_pulizia_se_aperta(self):
        if self.finestra_guida_pulizia is None or not self.finestra_guida_pulizia.winfo_exists():
            return
        x, y, larghezza, altezza = self._calcola_posizione_guida_pulizia()
        self.finestra_guida_pulizia.geometry(f"{larghezza}x{altezza}+{x}+{y}")

    @staticmethod
    def _area_lavoro_basso() -> int:
        """Bordo inferiore vero dello schermo, ESCLUSA la barra delle
        applicazioni di Windows (winfo_screenheight() include invece
        anche la barra, facendo credere che ci sia piu' spazio libero di
        quanto ce ne sia davvero)."""
        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
            return rect.bottom
        except (OSError, AttributeError):
            return 0

    def _rettangolo_finestra_vero(self) -> tuple:
        """Rettangolo VERO di ManagerShow (bordi e barra del titolo
        compresi), quello che corrisponde esattamente a cosa si vede a
        video e a cosa controlla geometry(). winfo_id() di Tkinter
        ritorna l'handle della sola area contenuto, non della finestra
        di primo livello: GetWindowRect chiamato direttamente su quello
        da' un rettangolo piu' piccolo (senza titolo ne' bordi), sfalsato
        rispetto alla finestra vera. GetAncestor(..., GA_ROOT) risale
        prima alla vera finestra di primo livello - verificato dal vivo
        che cosi' il rettangolo coincide esattamente con la geometria
        richiesta."""
        try:
            GA_ROOT = 2
            hwnd_root = ctypes.windll.user32.GetAncestor(
                ctypes.wintypes.HWND(self.winfo_id()), GA_ROOT
            )
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(ctypes.wintypes.HWND(hwnd_root), ctypes.byref(rect))
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
        except (OSError, AttributeError):
            pass
        return self.winfo_x(), self.winfo_y(), self.winfo_x() + self.winfo_width(), self.winfo_y() + self.winfo_height()

    def _calcola_posizione_guida_pulizia(self):
        self.update_idletasks()
        sinistra, _, destra, basso = self._rettangolo_finestra_vero()
        x = sinistra
        # destra-sinistra e' la larghezza VERA (bordi compresi) di
        # ManagerShow, ma geometry() sulla guida imposta solo l'area
        # contenuto: senza correzione la guida risulterebbe piu' larga di
        # ManagerShow del bordo della finestra stessa (~16px su Windows).
        larghezza = (destra - sinistra) - 16

        area_basso = self._area_lavoro_basso() or self.winfo_screenheight()
        y = basso + 8
        altezza_desiderata = 380

        # La guida non deve MAI partire piu' in alto del bordo inferiore
        # VERO di ManagerShow (altrimenti Windows la ripiazzerebbe
        # sovrapposta per farla entrare a schermo): se sotto non c'e'
        # spazio per l'altezza desiderata, si riduce l'altezza, mai la
        # posizione. Margine di sicurezza abbondante (48px, non 8) sopra
        # alla barra applicazioni, cosi' bordo/pulsanti della finestra
        # restano visibili anche se il calcolo dell'area di lavoro non
        # fosse preciso al pixel su questo monitor.
        altezza = min(altezza_desiderata, max(area_basso - y - 48, 120))

        return x, y, larghezza, altezza

    def _apri_guida_pulizia(self):
        if self.finestra_guida_pulizia is not None and self.finestra_guida_pulizia.winfo_exists():
            self.finestra_guida_pulizia.lift()
            self.finestra_guida_pulizia.focus_set()
            return

        finestra = tk.Toplevel(self)
        finestra.title(TESTO_GUIDA_PULIZIA[self.lingua]["titolo"])
        finestra.transient(self)

        testo_widget = tk.Text(finestra, wrap="word", padx=12, pady=12, font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(finestra, orient="vertical", command=testo_widget.yview)
        testo_widget.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        testo_widget.pack(side="left", fill="both", expand=True)
        testo_widget.insert("1.0", TESTO_GUIDA_PULIZIA[self.lingua]["corpo"])
        testo_widget.configure(state="disabled")

        self.finestra_guida_pulizia = finestra
        self.finestra_guida_pulizia.protocol("WM_DELETE_WINDOW", self._chiudi_guida_pulizia)
        x, y, larghezza, altezza = self._calcola_posizione_guida_pulizia()
        self.finestra_guida_pulizia.geometry(f"{larghezza}x{altezza}+{x}+{y}")

    def _chiudi_guida_pulizia(self):
        if self.finestra_guida_pulizia is not None:
            self.finestra_guida_pulizia.destroy()
        self.finestra_guida_pulizia = None

    def _apri_pagina_voto(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        webbrowser.open(f"http://localhost:{porta}/vota")

    def _apri_pagina_benvenuto(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        webbrowser.open(f"http://localhost:{porta}/")

    def _test_multi_voto(self):
        if not AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO):
            messagebox.showwarning(
                self._t("ms_test_voto_richiede_avvio_titolo"), self._t("ms_test_voto_richiede_avvio_testo"),
            )
            return

        numero = simpledialog.askinteger(
            self._t("ms_test_voto_titolo_dialogo"), self._t("ms_test_voto_prompt"),
            minvalue=1, maxvalue=20, initialvalue=4, parent=self,
        )
        if not numero:
            return

        browser = trova_browser_con_profili()
        if not browser:
            messagebox.showerror(
                self._t("ms_test_voto_browser_non_trovato_titolo"), self._t("ms_test_voto_browser_non_trovato_testo"),
            )
            return

        # Cartella dei profili di test ripulita ad ogni uso: sono usa e
        # getta, servono solo per la durata di questo test.
        shutil.rmtree(CARTELLA_PROFILI_TEST_VOTO, ignore_errors=True)
        CARTELLA_PROFILI_TEST_VOTO.mkdir(parents=True, exist_ok=True)

        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        url = f"http://127.0.0.1:{porta}/vota"

        for indice in range(numero):
            profilo = CARTELLA_PROFILI_TEST_VOTO / f"finestra_{indice + 1}"
            subprocess.Popen([
                browser, f"--user-data-dir={profilo}", "--no-first-run", "--new-window", url,
            ])

        messagebox.showinfo(
            self._t("gc_fatto_titolo"), self._t("ms_test_voto_aperte_testo", numero=numero, porta=porta),
        )

    def _apri_risultati(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        token = parser.get(SEZIONE_VOTO, "Token_Amministratore", fallback="").strip()
        url = f"http://localhost:{porta}/risultati"
        if token:
            url += f"?token={token}"
        webbrowser.open(url)

    def ricarica_lista(self):
        for riga in self.albero.get_children():
            self.albero.delete(riga)

        parser = carica_parser()
        cartella_sequenze = leggi_cartella_sequenze(parser)
        cartelle_escluse = {c.lower() for c in leggi_cartelle_escluse(parser)}

        if not cartella_sequenze.is_dir():
            messagebox.showerror(
                self._t("gc_cartella_non_trovata_titolo"),
                self._t("gc_cartella_non_trovata_testo", percorso=cartella_sequenze),
            )
            return

        self._cache_anteprime = {}
        prossimo_numero = 1
        for cartella in sorted(p for p in cartella_sequenze.iterdir() if p.is_dir()):
            if cartella.name.strip().lower() == "01 intro":
                numero = 0
            else:
                numero = prossimo_numero
                prossimo_numero += 1
            info = anteprima_sequenza(cartella)
            self._cache_anteprime[cartella.name] = info

            esclusa = cartella.name.lower() in cartelle_escluse
            if not info["ha_audio"]:
                stato = self._t("gc_stato_senza_audio")
            elif esclusa:
                stato = self._t("gc_stato_esclusa")
            else:
                stato = self._t("gc_stato_votabile")

            trigger = leggi_trigger(parser, info["id"])
            nome_lor = leggi_nome_lor(parser, info["id"])
            self.albero.insert(
                "", "end", iid=cartella.name,
                values=(numero, info["titolo"], info["artista"], info["durata_testo"], trigger, nome_lor, stato),
            )

    def _cartella_selezionata(self) -> str:
        selezione = self.albero.selection()
        if not selezione:
            messagebox.showinfo(self._t("gc_nessuna_selezione_titolo"), self._t("gc_nessuna_selezione_testo"))
            return None
        return selezione[0]

    def _aggiungi(self):
        parser = carica_parser()
        cartella_sequenze = leggi_cartella_sequenze(parser)
        dialogo = DialogoCanzone(self, cartella_sequenze, self.lingua)
        self.wait_window(dialogo)
        if dialogo.risultato_salvato:
            self.ricarica_lista()

    def _modifica(self):
        nome_cartella = self._cartella_selezionata()
        if nome_cartella is None:
            return
        info = self._cache_anteprime.get(nome_cartella)
        if info is None or not info["ha_audio"]:
            messagebox.showwarning(self._t("gc_non_modificabile_titolo"), self._t("gc_non_modificabile_testo"))
            return

        parser = carica_parser()
        cartella_sequenze = leggi_cartella_sequenze(parser)
        dialogo = DialogoCanzone(self, cartella_sequenze, self.lingua, dati_esistenti=info)
        self.wait_window(dialogo)
        if dialogo.risultato_salvato:
            self.ricarica_lista()

    def _alterna_esclusione(self):
        nome_cartella = self._cartella_selezionata()
        if nome_cartella is None:
            return

        parser = carica_parser()
        cartelle_escluse = leggi_cartelle_escluse(parser)
        cartelle_escluse_lower = {c.lower() for c in cartelle_escluse}

        if nome_cartella.lower() in cartelle_escluse_lower:
            cartelle_escluse = {c for c in cartelle_escluse if c.lower() != nome_cartella.lower()}
            azione = self._t("gc_azione_inclusa")
        else:
            if not messagebox.askyesno(
                self._t("gc_conferma_esclusione_titolo"),
                self._t("gc_conferma_esclusione_testo", nome=nome_cartella),
            ):
                return
            cartelle_escluse.add(nome_cartella)
            azione = self._t("gc_azione_esclusa")

        scrivi_cartelle_escluse(parser, cartelle_escluse)
        salva_parser(parser)

        ok, errore = rigenera_catalogo_sicuro(self.lingua)
        if not ok:
            messagebox.showwarning(
                self._t("gc_attenzione_titolo"),
                self._t("gc_attenzione_testo", nome=nome_cartella, azione=azione, errore=errore),
            )

        self.ricarica_lista()

    def _rigenera_manuale(self):
        ok, errore = rigenera_catalogo_sicuro(self.lingua)
        if ok:
            messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("gc_fatto_testo"))
        else:
            messagebox.showerror(self._t("gc_errore_titolo"), self._t("gc_rigenerazione_fallita", errore=errore))
        self.ricarica_lista()

    # ------------------------------------------------------------------
    # Scheda Programmazione
    # ------------------------------------------------------------------
    @staticmethod
    def _crea_area_scorrevole(padre):
        """Avvolge il contenuto di una scheda in un Canvas+Scrollbar
        verticale: alcune schede (es. Programmazione) hanno piu' sezioni
        di quante ne stiano in altezza su schermi piccoli o con
        risoluzioni/DPI diversi (il progetto e' pubblico, non solo per
        il nostro monitor). Ritorna il frame interno su cui costruire
        il contenuto, esattamente come si farebbe su 'padre' diretto."""
        padre.rowconfigure(0, weight=1)
        padre.columnconfigure(0, weight=1)

        canvas = tk.Canvas(padre, highlightthickness=0)
        scrollbar = ttk.Scrollbar(padre, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        contenuto = ttk.Frame(canvas)
        id_finestra = canvas.create_window((0, 0), window=contenuto, anchor="nw")

        def _aggiorna_regione_scorrimento(evento=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        contenuto.bind("<Configure>", _aggiorna_regione_scorrimento)

        def _adatta_larghezza_contenuto(evento):
            canvas.itemconfigure(id_finestra, width=evento.width)
        canvas.bind("<Configure>", _adatta_larghezza_contenuto)

        def _scorri_con_rotellina(evento):
            canvas.yview_scroll(int(-1 * (evento.delta / 120)), "units")
        canvas.bind("<Enter>", lambda evento: canvas.bind_all("<MouseWheel>", _scorri_con_rotellina))
        canvas.bind("<Leave>", lambda evento: canvas.unbind_all("<MouseWheel>"))

        return contenuto

    def _costruisci_tab_programmazione(self, padre):
        padding = {"padx": 8, "pady": 4}
        padre = self._crea_area_scorrevole(padre)
        padre.columnconfigure(0, weight=1)

        frame_guida_bottone = ttk.Frame(padre)
        frame_guida_bottone.grid(row=0, column=1, sticky="ne", padx=8, pady=4)
        self.bottone_guida = ttk.Button(frame_guida_bottone, command=self._apri_guida)
        self.bottone_guida.pack()

        self.frame_stato_live = ttk.LabelFrame(padre)
        self.frame_stato_live.grid(row=0, column=0, sticky="ew", **padding)

        # Un frame per riga con pack (non grid condiviso con altre righe):
        # cosi' pallino e testo restano sempre incollati, senza che la
        # larghezza di colonna venga influenzata da nient'altro nel box.
        riga_voto = ttk.Frame(self.frame_stato_live)
        riga_voto.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=6)
        self.pallino_voto = tk.Label(riga_voto, text="  ", bg="#c62828", width=1)
        self.pallino_voto.pack(side="left")
        self.var_stato_voto = tk.StringVar(value="VotoShow.py: ...")
        tk.Label(riga_voto, textvariable=self.var_stato_voto, padx=0).pack(side="left", padx=(4, 0))

        riga_motore = ttk.Frame(self.frame_stato_live)
        riga_motore.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        self.pallino_motore = tk.Label(riga_motore, text="  ", bg="#9e9e9e", width=1)
        self.pallino_motore.pack(side="left")
        self.var_stato_motore = tk.StringVar(value="MotoreShow.py: ...")
        tk.Label(riga_motore, textvariable=self.var_stato_motore, padx=0).pack(side="left", padx=(4, 0))

        frame_bottoni_stato_live = ttk.Frame(self.frame_stato_live)
        frame_bottoni_stato_live.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 4))
        self.bottone_avvia_ferma = ttk.Button(frame_bottoni_stato_live, command=self._alterna_avvio_ferma)
        self.bottone_avvia_ferma.pack(side="left")
        self.bottone_apri_voto = ttk.Button(frame_bottoni_stato_live, command=self._apri_pagina_voto)
        self.bottone_apri_voto.pack(side="left", padx=(4, 0))
        self.bottone_apri_risultati = ttk.Button(frame_bottoni_stato_live, command=self._apri_risultati)
        self.bottone_apri_risultati.pack(side="left", padx=(4, 0))
        self.bottone_apri_benvenuto = ttk.Button(frame_bottoni_stato_live, command=self._apri_pagina_benvenuto)
        self.bottone_apri_benvenuto.pack(side="left", padx=(4, 0))
        self.bottone_test_multi_voto = ttk.Button(frame_bottoni_stato_live, command=self._test_multi_voto)
        self.bottone_test_multi_voto.pack(side="left", padx=(16, 0))

        self.label_avviso_avvia_ferma = ttk.Label(
            self.frame_stato_live, foreground="#888", justify="left", wraplength=520,
        )
        self.label_avviso_avvia_ferma.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        self.frame_orari = ttk.LabelFrame(padre)
        self.frame_orari.grid(row=1, column=0, sticky="ew", **padding)

        self.label_accensione = ttk.Label(self.frame_orari)
        self.label_accensione.grid(row=0, column=0, sticky="w", **padding)
        self.var_accensione = tk.StringVar()
        ttk.Entry(self.frame_orari, textvariable=self.var_accensione, width=10).grid(row=0, column=1, sticky="w", **padding)

        self.label_spegnimento = ttk.Label(self.frame_orari)
        self.label_spegnimento.grid(row=1, column=0, sticky="w", **padding)
        self.var_spegnimento = tk.StringVar()
        ttk.Entry(self.frame_orari, textvariable=self.var_spegnimento, width=10).grid(row=1, column=1, sticky="w", **padding)

        self.label_giorni = ttk.Label(self.frame_orari)
        self.label_giorni.grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))
        frame_checkbox_giorni = ttk.Frame(self.frame_orari)
        frame_checkbox_giorni.grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 4))
        self.var_giorni = [tk.BooleanVar() for _ in GIORNI_SETTIMANA]
        self.checkbutton_giorni = []
        for indice in range(len(GIORNI_SETTIMANA)):
            checkbutton = ttk.Checkbutton(frame_checkbox_giorni, variable=self.var_giorni[indice])
            checkbutton.grid(row=0, column=indice, padx=4)
            self.checkbutton_giorni.append(checkbutton)

        frame_bottoni_orari = ttk.Frame(self.frame_orari)
        frame_bottoni_orari.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 8))
        self.bottone_salva_orari = ttk.Button(frame_bottoni_orari, command=self._salva_orari_bottone)
        self.bottone_salva_orari.pack(side="left", padx=(0, 4))
        self.bottone_pianificazione = ttk.Button(frame_bottoni_orari, command=self._alterna_pianificazione)
        self.bottone_pianificazione.pack(side="left", padx=4)

        self.var_stato_pianificazione = tk.StringVar(value="...")
        self.label_stato_pianificazione = ttk.Label(
            frame_bottoni_orari, textvariable=self.var_stato_pianificazione, justify="left", wraplength=340,
        )
        self.label_stato_pianificazione.pack(side="left", padx=(8, 0))

        self.frame_pulizia = ttk.LabelFrame(padre)
        self.frame_pulizia.grid(row=2, column=0, sticky="ew", **padding)
        self.label_minuti_anticipo = ttk.Label(self.frame_pulizia)
        self.label_minuti_anticipo.grid(row=0, column=0, sticky="w", **padding)
        self.var_minuti_anticipo = tk.StringVar()
        ttk.Entry(self.frame_pulizia, textvariable=self.var_minuti_anticipo, width=6).grid(
            row=0, column=1, sticky="w", padx=(4, 4), pady=4
        )
        self.bottone_pulisci_ora = ttk.Button(self.frame_pulizia, command=self._pulisci_ora)
        self.bottone_pulisci_ora.grid(row=0, column=2, padx=(0, 4))
        self.bottone_guida_pulizia = ttk.Button(self.frame_pulizia, command=self._apri_guida_pulizia)
        self.bottone_guida_pulizia.grid(row=0, column=3, padx=(0, 8))

        self.var_chiudi_chrome = tk.BooleanVar()
        self.checkbutton_chiudi_chrome = ttk.Checkbutton(self.frame_pulizia, variable=self.var_chiudi_chrome)
        self.checkbutton_chiudi_chrome.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 2))

        self.var_pulisci_cache = tk.BooleanVar()
        self.checkbutton_pulisci_cache = ttk.Checkbutton(self.frame_pulizia, variable=self.var_pulisci_cache)
        self.checkbutton_pulisci_cache.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 2))

        self.var_blocca_aggiornamenti = tk.BooleanVar()
        self.checkbutton_blocca_aggiornamenti = ttk.Checkbutton(
            self.frame_pulizia, variable=self.var_blocca_aggiornamenti,
        )
        self.checkbutton_blocca_aggiornamenti.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        self.programmi_extra = []
        frame_programmi_extra_label = ttk.Frame(self.frame_pulizia)
        frame_programmi_extra_label.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 0))
        self.label_programmi_extra = ttk.Label(frame_programmi_extra_label)
        self.label_programmi_extra.pack(side="left")
        self.bottone_aggiungi_programma = ttk.Button(
            frame_programmi_extra_label, command=self._apri_gestione_programmi_extra,
        )
        self.bottone_aggiungi_programma.pack(side="left", padx=(4, 0))

        self.var_programmi_extra_testo = tk.StringVar(value="-")
        ttk.Label(
            self.frame_pulizia, textvariable=self.var_programmi_extra_testo, foreground="#888", wraplength=460,
        ).grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

        self.frame_modalita_coda = ttk.LabelFrame(padre)
        self.frame_modalita_coda.grid(row=3, column=0, sticky="ew", **padding)

        self.label_modalita_coda = ttk.Label(self.frame_modalita_coda)
        self.label_modalita_coda.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_modalita_coda_display = tk.StringVar()
        self.combo_modalita_coda = ttk.Combobox(
            self.frame_modalita_coda, textvariable=self.var_modalita_coda_display,
            state="readonly", width=32, values=(),
        )
        self.combo_modalita_coda.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 4))
        self.combo_modalita_coda.bind("<<ComboboxSelected>>", self._aggiorna_pannello_modalita_coda)

        self.label_tetto_coda = ttk.Label(self.frame_modalita_coda)
        self.label_tetto_coda.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 4))
        self.var_tetto_coda = tk.StringVar()
        self.entry_tetto_coda = ttk.Entry(self.frame_modalita_coda, textvariable=self.var_tetto_coda, width=6)
        self.entry_tetto_coda.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=(0, 4))

        frame_raffreddamento = ttk.Frame(self.frame_modalita_coda)
        frame_raffreddamento.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 4))
        self.label_raffreddamento = ttk.Label(frame_raffreddamento)
        self.label_raffreddamento.pack(side="left")
        self.var_raffreddamento_tipo_display = tk.StringVar()
        self.combo_raffreddamento_tipo = ttk.Combobox(
            frame_raffreddamento, textvariable=self.var_raffreddamento_tipo_display,
            state="readonly", width=20, values=(),
        )
        self.combo_raffreddamento_tipo.pack(side="left", padx=(4, 12))
        self.label_raffreddamento_valore = ttk.Label(frame_raffreddamento)
        self.label_raffreddamento_valore.pack(side="left")
        self.var_raffreddamento_valore = tk.StringVar()
        ttk.Entry(frame_raffreddamento, textvariable=self.var_raffreddamento_valore, width=6).pack(
            side="left", padx=(4, 0)
        )

        self.label_nota_modalita_coda = ttk.Label(
            self.frame_modalita_coda, foreground="#1565c0", justify="left", wraplength=680,
            font=("Segoe UI", 9, "bold"),
        )
        self.label_nota_modalita_coda.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 10))

        self.bottone_salva_modalita_coda = ttk.Button(
            self.frame_modalita_coda, command=self._salva_modalita_coda,
        )
        self.bottone_salva_modalita_coda.grid(row=4, column=0, sticky="w", padx=8, pady=(6, 10))

        self.frame_token_admin = ttk.LabelFrame(padre)
        self.frame_token_admin.grid(row=4, column=0, sticky="ew", **padding)

        self.label_token_admin = ttk.Label(self.frame_token_admin)
        self.label_token_admin.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_token_admin = tk.StringVar()
        self.entry_token_admin = ttk.Entry(
            self.frame_token_admin, textvariable=self.var_token_admin, width=32, show="*",
        )
        self.entry_token_admin.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 4))

        frame_bottoni_token_admin = ttk.Frame(self.frame_token_admin)
        frame_bottoni_token_admin.grid(row=0, column=2, sticky="w", padx=(0, 8), pady=(8, 4))
        self.token_admin_visibile = False
        self.bottone_mostra_token_admin = ttk.Button(
            frame_bottoni_token_admin, command=self._alterna_visibilita_token_admin,
        )
        self.bottone_mostra_token_admin.pack(side="left", padx=(0, 4))
        self.bottone_genera_token_admin = ttk.Button(
            frame_bottoni_token_admin, command=self._genera_token_admin,
        )
        self.bottone_genera_token_admin.pack(side="left")

        self.label_nota_token_admin = ttk.Label(
            self.frame_token_admin, foreground="#888", justify="left", wraplength=680,
        )
        self.label_nota_token_admin.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 10))

        self.bottone_salva_token_admin = ttk.Button(
            self.frame_token_admin, command=self._salva_token_admin,
        )
        self.bottone_salva_token_admin.grid(row=2, column=0, sticky="w", padx=8, pady=(0, 10))

        self._carica_orari_da_ini()
        self._carica_modalita_coda_da_ini()
        self._carica_token_admin_da_ini()

    def _costruisci_tab_saluto_babbonatale(self, padre):
        padding = {"padx": 8, "pady": 4}
        padre.columnconfigure(0, weight=1)

        frame_guida_bottone = ttk.Frame(padre)
        frame_guida_bottone.grid(row=0, column=1, sticky="ne", padx=8, pady=4)
        self.bottone_guida_saluto_babbonatale = ttk.Button(frame_guida_bottone, command=self._apri_guida)
        self.bottone_guida_saluto_babbonatale.pack()

        self.frame_saluto_babbonatale = ttk.LabelFrame(padre)
        self.frame_saluto_babbonatale.grid(row=1, column=0, sticky="ew", **padding)

        self.var_saluto_babbonatale_abilitato = tk.BooleanVar()
        self.checkbutton_saluto_babbonatale = ttk.Checkbutton(
            self.frame_saluto_babbonatale, variable=self.var_saluto_babbonatale_abilitato,
        )
        self.checkbutton_saluto_babbonatale.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self.label_nota_saluto_babbonatale = ttk.Label(
            self.frame_saluto_babbonatale, foreground="#888", justify="left", wraplength=680,
        )
        self.label_nota_saluto_babbonatale.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 10))

        self.bottone_salva_saluto_babbonatale = ttk.Button(
            self.frame_saluto_babbonatale, command=self._salva_saluto_babbonatale,
        )
        self.bottone_salva_saluto_babbonatale.grid(row=2, column=0, sticky="w", padx=8, pady=(0, 10))

        self._carica_saluto_babbonatale_da_ini()

    # Ordine fisso delle modalita' (chiave salvata in ini -> chiave di
    # traduzione dell'etichetta mostrata nel combobox / della nota).
    MODALITA_CODA_CHIAVI = ("turno", "persistente", "tetto")
    MODALITA_CODA_CHIAVE_TRADUZIONE = {
        "turno": "ms_modalita_coda_turno",
        "persistente": "ms_modalita_coda_persistente",
        "tetto": "ms_modalita_coda_tetto",
    }
    MODALITA_CODA_CHIAVE_NOTA = {
        "turno": "ms_nota_modalita_coda_turno",
        "persistente": "ms_nota_modalita_coda_persistente",
        "tetto": "ms_nota_modalita_coda_tetto",
    }
    RAFFREDDAMENTO_TIPO_CHIAVI = ("turni", "minuti")
    RAFFREDDAMENTO_TIPO_CHIAVE_TRADUZIONE = {
        "turni": "ms_raffreddamento_turni",
        "minuti": "ms_raffreddamento_minuti",
    }

    def _etichette_raffreddamento_tipo(self) -> list:
        return [self._t(self.RAFFREDDAMENTO_TIPO_CHIAVE_TRADUZIONE[chiave]) for chiave in self.RAFFREDDAMENTO_TIPO_CHIAVI]

    def _chiave_da_etichetta_raffreddamento_tipo(self, etichetta: str) -> str:
        for chiave in self.RAFFREDDAMENTO_TIPO_CHIAVI:
            if self._t(self.RAFFREDDAMENTO_TIPO_CHIAVE_TRADUZIONE[chiave]) == etichetta:
                return chiave
        return "turni"

    def _etichette_modalita_coda(self) -> list:
        return [self._t(self.MODALITA_CODA_CHIAVE_TRADUZIONE[chiave]) for chiave in self.MODALITA_CODA_CHIAVI]

    def _chiave_da_etichetta_modalita_coda(self, etichetta: str) -> str:
        for chiave in self.MODALITA_CODA_CHIAVI:
            if self._t(self.MODALITA_CODA_CHIAVE_TRADUZIONE[chiave]) == etichetta:
                return chiave
        return "persistente"

    def _carica_modalita_coda_da_ini(self):
        parser = carica_parser()
        modalita = parser.get(SEZIONE_VOTO, "Modalita_Coda", fallback="persistente").strip().lower()
        if modalita not in self.MODALITA_CODA_CHIAVI:
            modalita = "persistente"
        tetto = parser.get(SEZIONE_VOTO, "Tetto_Coda", fallback="3").strip()

        self.combo_modalita_coda.configure(values=self._etichette_modalita_coda())
        self.var_modalita_coda_display.set(self._t(self.MODALITA_CODA_CHIAVE_TRADUZIONE[modalita]))
        self.var_tetto_coda.set(tetto)

        tipo_raffreddamento = parser.get(SEZIONE_VOTO, "Raffreddamento_Tipo", fallback="turni").strip().lower()
        if tipo_raffreddamento not in self.RAFFREDDAMENTO_TIPO_CHIAVI:
            tipo_raffreddamento = "turni"
        valore_raffreddamento = parser.get(SEZIONE_VOTO, "Raffreddamento_Valore", fallback="3").strip()

        self.combo_raffreddamento_tipo.configure(values=self._etichette_raffreddamento_tipo())
        self.var_raffreddamento_tipo_display.set(self._t(self.RAFFREDDAMENTO_TIPO_CHIAVE_TRADUZIONE[tipo_raffreddamento]))
        self.var_raffreddamento_valore.set(valore_raffreddamento)

        self._aggiorna_pannello_modalita_coda()

    def _aggiorna_pannello_modalita_coda(self, evento=None):
        chiave = self._chiave_da_etichetta_modalita_coda(self.var_modalita_coda_display.get())
        self.entry_tetto_coda.configure(state="normal" if chiave == "tetto" else "disabled")
        self.label_nota_modalita_coda.configure(text=self._t(self.MODALITA_CODA_CHIAVE_NOTA[chiave]))

    def _salva_modalita_coda(self):
        chiave = self._chiave_da_etichetta_modalita_coda(self.var_modalita_coda_display.get())
        tetto_testo = self.var_tetto_coda.get().strip()

        if chiave == "tetto":
            if not tetto_testo.isdigit() or int(tetto_testo) < 1:
                messagebox.showerror(self._t("ms_errore_tetto_titolo"), self._t("ms_errore_tetto_testo"))
                return
        elif not tetto_testo.isdigit():
            tetto_testo = "3"  # non usato in questa modalita', ma un valore valido evita un ini sporco

        tipo_raffreddamento = self._chiave_da_etichetta_raffreddamento_tipo(self.var_raffreddamento_tipo_display.get())
        valore_raffreddamento_testo = self.var_raffreddamento_valore.get().strip()
        if not valore_raffreddamento_testo.isdigit() or int(valore_raffreddamento_testo) < 1:
            messagebox.showerror(self._t("ms_errore_raffreddamento_titolo"), self._t("ms_errore_raffreddamento_testo"))
            return

        parser = carica_parser()
        parser[SEZIONE_VOTO]["Modalita_Coda"] = chiave
        parser[SEZIONE_VOTO]["Tetto_Coda"] = tetto_testo
        parser[SEZIONE_VOTO]["Raffreddamento_Tipo"] = tipo_raffreddamento
        parser[SEZIONE_VOTO]["Raffreddamento_Valore"] = valore_raffreddamento_testo
        salva_parser(parser)

        if AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO):
            if messagebox.askyesno(
                self._t("ms_modalita_coda_riavvia_ora_titolo"), self._t("ms_modalita_coda_riavvia_ora_testo"),
            ):
                self._riavvia_votoshow_silenzioso()
                messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_modalita_coda_riavviato_testo"))
                return

        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_modalita_coda_salvata_testo"))

    def _carica_saluto_babbonatale_da_ini(self):
        parser = carica_parser()
        abilitato = parser.getboolean(SEZIONE_SALUTO_BABBONATALE, "Abilita", fallback=False)
        self.var_saluto_babbonatale_abilitato.set(abilitato)

    def _salva_saluto_babbonatale(self):
        parser = carica_parser()
        parser[SEZIONE_SALUTO_BABBONATALE]["Abilita"] = "True" if self.var_saluto_babbonatale_abilitato.get() else "False"
        salva_parser(parser)

        if AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO):
            if messagebox.askyesno(
                self._t("ms_saluto_babbonatale_riavvia_ora_titolo"), self._t("ms_saluto_babbonatale_riavvia_ora_testo"),
            ):
                self._riavvia_votoshow_silenzioso()
                messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_saluto_babbonatale_riavviato_testo"))
                return

        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_saluto_babbonatale_salvata_testo"))

    def _carica_token_admin_da_ini(self):
        parser = carica_parser()
        token = parser.get(SEZIONE_VOTO, "Token_Amministratore", fallback="").strip()
        self.var_token_admin.set(token)

    def _alterna_visibilita_token_admin(self):
        self.token_admin_visibile = not self.token_admin_visibile
        self.entry_token_admin.configure(show="" if self.token_admin_visibile else "*")
        self.bottone_mostra_token_admin.configure(
            text=self._t("ms_bottone_nascondi_token" if self.token_admin_visibile else "ms_bottone_mostra_token")
        )

    def _genera_token_admin(self):
        self.var_token_admin.set(secrets.token_urlsafe(16))
        if not self.token_admin_visibile:
            self._alterna_visibilita_token_admin()

    def _salva_token_admin(self):
        token = self.var_token_admin.get().strip()
        if not token:
            if not messagebox.askyesno(
                self._t("ms_token_admin_vuoto_titolo"), self._t("ms_token_admin_vuoto_testo"),
            ):
                return

        parser = carica_parser()
        parser[SEZIONE_VOTO]["Token_Amministratore"] = token
        salva_parser(parser)

        if AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO):
            if messagebox.askyesno(
                self._t("ms_token_admin_riavvia_ora_titolo"), self._t("ms_token_admin_riavvia_ora_testo"),
            ):
                self._riavvia_votoshow_silenzioso()
                messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_token_admin_riavviato_testo"))
                return

        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_token_admin_salvato_testo"))

    def _riavvia_votoshow_silenzioso(self):
        """Stop+avvio di VotoShow.py/MotoreShow.py senza i popup di
        conferma di _ferma_ora()/_avvia_ora() (qui il riavvio e' un
        passaggio intermedio, non l'azione che l'utente ha chiesto).
        FermaShow.ferma() e AvviaShow.avvia() verificano entrambi lo
        stato reale della porta (non solo il PID), quindi non serve
        piu' una pausa fissa a indovinare i tempi di Windows."""
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        FermaShow.ferma(FermaShow.FILE_PID_VOTO, porta=porta)
        FermaShow.ferma(FermaShow.FILE_PID_MOTORE)
        AvviaShow.avvia(AvviaShow.FILE_VOTOSHOW, AvviaShow.FILE_PID_VOTO, porta=porta)
        AvviaShow.avvia(AvviaShow.FILE_MOTORESHOW, AvviaShow.FILE_PID_MOTORE)
        self._aggiorna_stato_processi()

    def _carica_orari_da_ini(self):
        parser = carica_parser()
        orari = parser[SEZIONE_ORARI]
        self.var_accensione.set(orari.get("Orario_Accensione", "17:30"))
        self.var_spegnimento.set(orari.get("Orario_Spegnimento", "22:30"))

        giorni_raw = orari.get("Giorni_Attivi", "0,1,2,3,4,5,6")
        giorni_salvati = {g.strip() for g in giorni_raw.split(",") if g.strip()}
        for indice in range(len(GIORNI_SETTIMANA)):
            self.var_giorni[indice].set(str(indice) in giorni_salvati)

        pulizia = parser[SEZIONE_PULIZIA] if SEZIONE_PULIZIA in parser else {}
        self.var_minuti_anticipo.set(str(pulizia.get("Minuti_Anticipo", "15")))
        self.var_chiudi_chrome.set(
            SEZIONE_PULIZIA not in parser or parser.getboolean(SEZIONE_PULIZIA, "Chiudi_Chrome", fallback=True)
        )
        self.var_pulisci_cache.set(
            SEZIONE_PULIZIA not in parser or parser.getboolean(SEZIONE_PULIZIA, "Pulisci_File_Temporanei", fallback=True)
        )
        self.var_blocca_aggiornamenti.set(
            SEZIONE_PULIZIA in parser and parser.getboolean(SEZIONE_PULIZIA, "Blocca_Aggiornamenti_Windows", fallback=False)
        )
        grezzo_programmi = pulizia.get("Programmi_Extra", "").strip() if pulizia else ""
        self.programmi_extra = [p.strip() for p in grezzo_programmi.split(",") if p.strip()]
        self._aggiorna_etichetta_programmi_extra()

    def _aggiorna_etichetta_programmi_extra(self):
        self.var_programmi_extra_testo.set(", ".join(self.programmi_extra) if self.programmi_extra else "-")

    @staticmethod
    def _orario_valido(testo_orario: str) -> bool:
        try:
            datetime.strptime(testo_orario.strip(), "%H:%M")
            return True
        except ValueError:
            return False

    def _valida_orari(self) -> bool:
        if not self._orario_valido(self.var_accensione.get()):
            messagebox.showerror(self._t("ms_errore_orario_titolo"), self._t("ms_errore_orario_accensione_testo"))
            return False
        if not self._orario_valido(self.var_spegnimento.get()):
            messagebox.showerror(self._t("ms_errore_orario_titolo"), self._t("ms_errore_orario_spegnimento_testo"))
            return False
        if not any(v.get() for v in self.var_giorni):
            messagebox.showerror(self._t("ms_errore_nessun_giorno_titolo"), self._t("ms_errore_nessun_giorno_testo"))
            return False
        if not self.var_minuti_anticipo.get().strip().isdigit():
            messagebox.showerror(self._t("ms_errore_minuti_titolo"), self._t("ms_errore_minuti_testo"))
            return False
        return True

    def _salva_orari_su_ini(self) -> bool:
        if not self._valida_orari():
            return False
        parser = carica_parser()
        parser[SEZIONE_ORARI]["Orario_Accensione"] = self.var_accensione.get().strip()
        parser[SEZIONE_ORARI]["Orario_Spegnimento"] = self.var_spegnimento.get().strip()
        giorni_selezionati = [str(i) for i, v in enumerate(self.var_giorni) if v.get()]
        parser[SEZIONE_ORARI]["Giorni_Attivi"] = ",".join(giorni_selezionati)
        if SEZIONE_PULIZIA not in parser:
            parser[SEZIONE_PULIZIA] = {}
        parser[SEZIONE_PULIZIA]["Minuti_Anticipo"] = self.var_minuti_anticipo.get().strip()
        parser[SEZIONE_PULIZIA]["Chiudi_Chrome"] = str(self.var_chiudi_chrome.get())
        parser[SEZIONE_PULIZIA]["Pulisci_File_Temporanei"] = str(self.var_pulisci_cache.get())
        parser[SEZIONE_PULIZIA]["Blocca_Aggiornamenti_Windows"] = str(self.var_blocca_aggiornamenti.get())
        parser[SEZIONE_PULIZIA]["Programmi_Extra"] = ", ".join(self.programmi_extra)
        salva_parser(parser)
        return True

    def _salva_orari_bottone(self):
        if self._salva_orari_su_ini():
            messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_salva_orari_promemoria_testo"))

    def _applica_pianificazione(self):
        if not self._salva_orari_su_ini():
            return

        giorni_indici = [i for i, v in enumerate(self.var_giorni) if v.get()]
        giorni_schtasks = [MAPPA_GIORNI_SCHTASKS[i] for i in giorni_indici]
        orario_pulizia = _calcola_orario_anticipato(
            self.var_accensione.get().strip(), int(self.var_minuti_anticipo.get().strip())
        )

        ok_avvio, errore_avvio = _crea_task(
            NOME_TASK_AVVIO, CARTELLA_AUTOMAZIONE / "AvviaShow.py", giorni_schtasks, self.var_accensione.get().strip()
        )
        ok_stop, errore_stop = _crea_task(
            NOME_TASK_STOP, CARTELLA_AUTOMAZIONE / "FermaShow.py", giorni_schtasks, self.var_spegnimento.get().strip()
        )
        ok_pulizia, errore_pulizia = _crea_task(
            NOME_TASK_PULIZIA, CARTELLA_AUTOMAZIONE / "PuliziaPreShow.py", giorni_schtasks, orario_pulizia
        )

        if ok_avvio and ok_stop and ok_pulizia:
            messagebox.showinfo(
                self._t("gc_fatto_titolo"),
                self._t("ms_pianificazione_applicata_testo", orario=orario_pulizia),
            )
            if self.var_blocca_aggiornamenti.get() and not _processo_e_elevato():
                messagebox.showwarning(
                    self._t("ms_avviso_no_admin_titolo"),
                    self._t("ms_avviso_no_admin_testo"),
                )
        else:
            dettagli = []
            if not ok_avvio:
                dettagli.append(f"{self._t('ms_prefisso_avvio')}: {errore_avvio}")
            if not ok_stop:
                dettagli.append(f"{self._t('ms_prefisso_stop')}: {errore_stop}")
            if not ok_pulizia:
                dettagli.append(f"{self._t('ms_prefisso_pulizia')}: {errore_pulizia}")
            messagebox.showerror(
                self._t("gc_errore_titolo"),
                self._t("ms_pianificazione_fallita_testo", dettagli="\n".join(dettagli)),
            )

        self._aggiorna_stato_pianificazione()

    def _rimuovi_pianificazione(self):
        if not messagebox.askyesno(self._t("ms_conferma_titolo"), self._t("ms_conferma_rimozione_testo")):
            return
        _elimina_task(NOME_TASK_PULIZIA)
        _elimina_task(NOME_TASK_AVVIO)
        _elimina_task(NOME_TASK_STOP)
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_pianificazione_rimossa_testo"))
        self._aggiorna_stato_pianificazione()

    def _alterna_pianificazione(self):
        if _task_esiste(NOME_TASK_AVVIO) and _task_esiste(NOME_TASK_STOP) and _task_esiste(NOME_TASK_PULIZIA):
            self._rimuovi_pianificazione()
        else:
            self._applica_pianificazione()

    def _aggiorna_stato_pianificazione(self):
        registrato_avvio = _task_esiste(NOME_TASK_AVVIO)
        registrato_stop = _task_esiste(NOME_TASK_STOP)
        registrato_pulizia = _task_esiste(NOME_TASK_PULIZIA)
        if registrato_avvio and registrato_stop and registrato_pulizia:
            self.var_stato_pianificazione.set(self._t("ms_stato_pianificazione_attiva"))
            self.label_stato_pianificazione.configure(foreground="#2e7d32")
            self.bottone_pianificazione.configure(text=self._t("ms_bottone_rimuovi"))
        else:
            self.var_stato_pianificazione.set(self._t("ms_stato_pianificazione_non_attiva"))
            self.label_stato_pianificazione.configure(foreground="#c62828")
            self.bottone_pianificazione.configure(text=self._t("ms_bottone_applica"))

    def _pulisci_ora(self):
        PuliziaPreShow.esegui_pulizia()
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_pulizia_eseguita_testo"))

    def _apri_gestione_programmi_extra(self):
        finestra = tk.Toplevel(self)
        finestra.title(self._t("ms_titolo_programmi_extra"))
        finestra.resizable(False, False)
        finestra.transient(self)
        finestra.grab_set()

        ttk.Label(finestra, text=self._t("ms_istruzioni_programmi_extra"), wraplength=340, justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4)
        )

        lista = tk.Listbox(finestra, height=8, width=40)
        lista.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        for nome in self.programmi_extra:
            lista.insert("end", nome)

        def _sfoglia():
            cartella_iniziale = os.environ.get("ProgramFiles", "C:/")
            percorso = filedialog.askopenfilename(
                title=self._t("ms_titolo_programmi_extra"),
                initialdir=cartella_iniziale,
                filetypes=[(self._t("ms_filtro_eseguibili"), "*.exe"), (self._t("gc_filtro_tutti"), "*.*")],
            )
            if not percorso:
                return
            nome = Path(percorso).name
            if nome not in lista.get(0, "end"):
                lista.insert("end", nome)

        def _rimuovi():
            selezione = lista.curselection()
            if selezione:
                lista.delete(selezione[0])

        def _salva_e_chiudi():
            self.programmi_extra = list(lista.get(0, "end"))
            self._aggiorna_etichetta_programmi_extra()
            parser = carica_parser()
            if SEZIONE_PULIZIA not in parser:
                parser[SEZIONE_PULIZIA] = {}
            parser[SEZIONE_PULIZIA]["Programmi_Extra"] = ", ".join(self.programmi_extra)
            salva_parser(parser)
            finestra.destroy()

        frame_sfoglia_rimuovi = ttk.Frame(finestra)
        frame_sfoglia_rimuovi.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Button(frame_sfoglia_rimuovi, text=self._t("ms_bottone_sfoglia_programma"), command=_sfoglia).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(frame_sfoglia_rimuovi, text=self._t("ms_bottone_rimuovi_programma"), command=_rimuovi).pack(
            side="left"
        )

        frame_chiudi = ttk.Frame(finestra)
        frame_chiudi.grid(row=3, column=0, columnspan=3, sticky="e", padx=8, pady=(4, 8))
        ttk.Button(frame_chiudi, text=self._t("gc_bottone_chiudi"), command=finestra.destroy).pack(side="left", padx=4)
        ttk.Button(frame_chiudi, text=self._t("gc_bottone_salva"), command=_salva_e_chiudi).pack(side="left")

        finestra.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - finestra.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - finestra.winfo_height()) // 2
        finestra.geometry(f"+{x}+{y}")

    def _disegna_pallino(self, pallino: tk.Label, colore: str):
        pallino.configure(bg=colore)

    def _aggiorna_stato_processi(self):
        voto_attivo = AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO)
        self._disegna_pallino(self.pallino_voto, "#2e7d32" if voto_attivo else "#c62828")
        stato_voto = self._t("ms_stato_in_esecuzione") if voto_attivo else self._t("ms_stato_fermo")
        self.var_stato_voto.set(f"VotoShow.py: {stato_voto}")
        testo_bottone_avvia_ferma = self._t("ms_bottone_ferma_ora") if voto_attivo else self._t("ms_bottone_avvia_ora")
        self.bottone_avvia_ferma.configure(text=testo_bottone_avvia_ferma)
        self.bottone_avvia_ferma_benvenuto.configure(text=testo_bottone_avvia_ferma)

        motore_attivo = AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_MOTORE)
        self._disegna_pallino(self.pallino_motore, "#2e7d32" if motore_attivo else "#9e9e9e")
        stato_motore = self._t("ms_stato_in_esecuzione") if motore_attivo else self._t("ms_stato_motore_concluso")
        self.var_stato_motore.set(f"MotoreShow.py: {stato_motore}")

        self.after(5000, self._aggiorna_stato_processi)

    def _alterna_avvio_ferma(self):
        if AvviaShow.verifica_in_esecuzione(AvviaShow.FILE_PID_VOTO):
            self._ferma_ora()
        else:
            self._avvia_ora()

    def _avvia_ora(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        AvviaShow.avvia(AvviaShow.FILE_VOTOSHOW, AvviaShow.FILE_PID_VOTO, porta=porta)
        AvviaShow.avvia(AvviaShow.FILE_MOTORESHOW, AvviaShow.FILE_PID_MOTORE)
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_avviati_testo"))
        self._aggiorna_stato_processi()

    def _ferma_ora(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        FermaShow.ferma(FermaShow.FILE_PID_VOTO, porta=porta)
        FermaShow.ferma(FermaShow.FILE_PID_MOTORE)
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_fermati_testo"))
        self._aggiorna_stato_processi()

    # ------------------------------------------------------------------
    # Scheda Benvenuto (pagina che gli ospiti vedono per prima, es. dopo
    # il redirect automatico del Fritz!Box, prima di arrivare al voto)
    # ------------------------------------------------------------------
    def _costruisci_tab_benvenuto(self, padre):
        padding = {"padx": 8, "pady": 4}
        padre.columnconfigure(0, weight=1)

        self.label_nota_benvenuto = ttk.Label(padre, foreground="#888", justify="left", wraplength=680)
        self.label_nota_benvenuto.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self.frame_testo_benvenuto = ttk.LabelFrame(padre)
        self.frame_testo_benvenuto.grid(row=1, column=0, sticky="ew", **padding)
        self.testo_benvenuto_widget = tk.Text(self.frame_testo_benvenuto, height=5, width=70, wrap="word")
        self.testo_benvenuto_widget.pack(fill="x", expand=True, padx=8, pady=(8, 0))
        self.label_nota_testo_benvenuto = ttk.Label(
            self.frame_testo_benvenuto, foreground="#888", justify="left", wraplength=640,
        )
        self.label_nota_testo_benvenuto.pack(fill="x", padx=8, pady=(4, 8))

        self.frame_immagine_benvenuto = ttk.LabelFrame(padre)
        self.frame_immagine_benvenuto.grid(row=2, column=0, sticky="ew", **padding)
        self.label_campo_immagine_benvenuto = ttk.Label(self.frame_immagine_benvenuto)
        self.label_campo_immagine_benvenuto.grid(row=0, column=0, sticky="w", **padding)
        self.var_nome_immagine_benvenuto = tk.StringVar()
        ttk.Label(self.frame_immagine_benvenuto, textvariable=self.var_nome_immagine_benvenuto, width=34).grid(
            row=0, column=1, sticky="w", **padding
        )
        self.bottone_sfoglia_immagine_benvenuto = ttk.Button(
            self.frame_immagine_benvenuto, command=self._sfoglia_immagine_benvenuto,
        )
        self.bottone_sfoglia_immagine_benvenuto.grid(row=0, column=2, **padding)
        self.bottone_rimuovi_immagine_benvenuto = ttk.Button(
            self.frame_immagine_benvenuto, command=self._rimuovi_immagine_benvenuto,
        )
        self.bottone_rimuovi_immagine_benvenuto.grid(row=0, column=3, **padding)

        self.frame_audio_benvenuto = ttk.LabelFrame(padre)
        self.frame_audio_benvenuto.grid(row=3, column=0, sticky="ew", **padding)
        self.label_campo_audio_benvenuto = ttk.Label(self.frame_audio_benvenuto)
        self.label_campo_audio_benvenuto.grid(row=0, column=0, sticky="w", **padding)
        self.var_nome_audio_benvenuto = tk.StringVar()
        ttk.Label(self.frame_audio_benvenuto, textvariable=self.var_nome_audio_benvenuto, width=34).grid(
            row=0, column=1, sticky="w", **padding
        )
        self.bottone_sfoglia_audio_benvenuto = ttk.Button(
            self.frame_audio_benvenuto, command=self._sfoglia_audio_benvenuto,
        )
        self.bottone_sfoglia_audio_benvenuto.grid(row=0, column=2, **padding)
        self.bottone_rimuovi_audio_benvenuto = ttk.Button(
            self.frame_audio_benvenuto, command=self._rimuovi_audio_benvenuto,
        )
        self.bottone_rimuovi_audio_benvenuto.grid(row=0, column=3, **padding)

        self.frame_colore_benvenuto = ttk.LabelFrame(padre)
        self.frame_colore_benvenuto.grid(row=4, column=0, sticky="ew", **padding)
        self.colore_benvenuto = COLORE_BENVENUTO_DEFAULT
        self.swatch_colore_benvenuto = tk.Label(
            self.frame_colore_benvenuto, text="      ", width=8, relief="sunken", bg=self.colore_benvenuto,
        )
        self.swatch_colore_benvenuto.grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.bottone_cambia_colore_benvenuto = ttk.Button(
            self.frame_colore_benvenuto, command=self._cambia_colore_benvenuto,
        )
        self.bottone_cambia_colore_benvenuto.grid(row=0, column=1, padx=4, pady=8)
        self.label_nota_colore_benvenuto = ttk.Label(
            self.frame_colore_benvenuto, foreground="#888", justify="left", wraplength=460,
        )
        self.label_nota_colore_benvenuto.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        frame_bottoni_benvenuto = ttk.Frame(padre)
        frame_bottoni_benvenuto.grid(row=5, column=0, sticky="w", padx=8, pady=(4, 8))
        self.bottone_salva_benvenuto = ttk.Button(frame_bottoni_benvenuto, command=self._salva_benvenuto)
        self.bottone_salva_benvenuto.pack(side="left", padx=(0, 4))
        # Stessa azione avvia/ferma di VotoShow.py della scheda Programmazione,
        # duplicata qui: dopo aver cambiato jpg/testo, riavviare per far
        # rileggere la config non richiede di cambiare scheda.
        self.bottone_avvia_ferma_benvenuto = ttk.Button(frame_bottoni_benvenuto, command=self._alterna_avvio_ferma)
        self.bottone_avvia_ferma_benvenuto.pack(side="left", padx=(0, 4))
        self.bottone_anteprima_benvenuto = ttk.Button(frame_bottoni_benvenuto, command=self._apri_pagina_benvenuto)
        self.bottone_anteprima_benvenuto.pack(side="left")

        self._carica_dati_benvenuto()

    def _carica_dati_benvenuto(self):
        parser = carica_parser()
        config = leggi_config_benvenuto(parser)

        self.testo_benvenuto_widget.delete("1.0", "end")
        self.testo_benvenuto_widget.insert("1.0", config["testo"])

        self.percorso_immagine_benvenuto_scelta = None
        self.rimuovi_immagine_benvenuto = False
        self.var_nome_immagine_benvenuto.set(config["immagine"] or self._t("ms_bv_nessuna_scelta"))

        self.percorso_audio_benvenuto_scelto = None
        self.rimuovi_audio_benvenuto = False
        self.var_nome_audio_benvenuto.set(config["audio"] or self._t("ms_bv_nessuna_scelta"))

        self._imposta_colore_benvenuto(config["colore"])

    def _sfoglia_immagine_benvenuto(self):
        percorso = filedialog.askopenfilename(
            title=self._t("ms_bv_scegli_immagine_titolo"),
            filetypes=[
                (self._t("ms_bv_filtro_immagini"), "*.jpg *.jpeg *.png *.gif"),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if not percorso:
            return
        self.percorso_immagine_benvenuto_scelta = Path(percorso)
        self.rimuovi_immagine_benvenuto = False
        self.var_nome_immagine_benvenuto.set(self.percorso_immagine_benvenuto_scelta.name)
        self._prova_estrai_colore_da_immagine(self.percorso_immagine_benvenuto_scelta)

    def _rimuovi_immagine_benvenuto(self):
        self.percorso_immagine_benvenuto_scelta = None
        self.rimuovi_immagine_benvenuto = True
        self.var_nome_immagine_benvenuto.set(self._t("ms_bv_nessuna_scelta"))

    def _prova_estrai_colore_da_immagine(self, percorso: Path):
        """Propone un colore per la fascia testo preso dall'immagine
        (media dei pixel, ridotta a 1x1 con Pillow): solo un suggerimento,
        l'utente puo' sempre cambiarlo a mano con 'Cambia colore...'.
        Pillow e' opzionale: se manca, si offre l'installazione automatica
        (stesso schema gia' usato per 'mutagen') ma non blocca il resto."""
        try:
            from PIL import Image
        except ImportError:
            vuole_installare = messagebox.askyesno(
                self._t("ms_bv_pillow_mancante_titolo"), self._t("ms_bv_pillow_mancante_testo"),
            )
            if vuole_installare:
                risultato = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pillow"], capture_output=True, text=True,
                )
                if risultato.returncode == 0:
                    messagebox.showinfo(self._t("gc_installazione_riuscita_titolo"), self._t("ms_bv_pillow_installata_testo"))
                else:
                    messagebox.showerror(
                        self._t("gc_installazione_fallita_titolo"),
                        self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
                    )
            return

        try:
            with Image.open(percorso) as immagine:
                r, g, b = immagine.convert("RGB").resize((1, 1)).getpixel((0, 0))
            self._imposta_colore_benvenuto(f"#{r:02x}{g:02x}{b:02x}")
        except Exception:
            pass  # immagine illeggibile: si tiene il colore attuale, resta comunque scegliebile a mano

    def _imposta_colore_benvenuto(self, colore_hex: str):
        self.colore_benvenuto = colore_hex
        self.swatch_colore_benvenuto.configure(bg=colore_hex)

    def _cambia_colore_benvenuto(self):
        scelto = colorchooser.askcolor(color=self.colore_benvenuto, title=self._t("ms_bv_bottone_cambia_colore"))
        if scelto and scelto[1]:
            self._imposta_colore_benvenuto(scelto[1])

    def _sfoglia_audio_benvenuto(self):
        percorso = filedialog.askopenfilename(
            title=self._t("ms_bv_scegli_audio_titolo"),
            filetypes=[
                (self._t("ms_bv_filtro_audio"), "*.mp3"),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if not percorso:
            return
        self.percorso_audio_benvenuto_scelto = Path(percorso)
        self.rimuovi_audio_benvenuto = False
        self.var_nome_audio_benvenuto.set(self.percorso_audio_benvenuto_scelto.name)

    def _rimuovi_audio_benvenuto(self):
        self.percorso_audio_benvenuto_scelto = None
        self.rimuovi_audio_benvenuto = True
        self.var_nome_audio_benvenuto.set(self._t("ms_bv_nessuna_scelta"))

    def _salva_benvenuto(self):
        testo_benvenuto = self.testo_benvenuto_widget.get("1.0", "end-1c")
        parser = carica_parser()
        precedente = leggi_config_benvenuto(parser)

        try:
            CARTELLA_BENVENUTO.mkdir(parents=True, exist_ok=True)

            nome_immagine = precedente["immagine"]
            if self.rimuovi_immagine_benvenuto:
                if nome_immagine:
                    (CARTELLA_BENVENUTO / nome_immagine).unlink(missing_ok=True)
                nome_immagine = ""
            elif self.percorso_immagine_benvenuto_scelta is not None:
                nome_immagine_nuovo = "sfondo" + self.percorso_immagine_benvenuto_scelta.suffix.lower()
                if nome_immagine and nome_immagine != nome_immagine_nuovo:
                    (CARTELLA_BENVENUTO / nome_immagine).unlink(missing_ok=True)
                nome_immagine = nome_immagine_nuovo
                shutil.copyfile(self.percorso_immagine_benvenuto_scelta, CARTELLA_BENVENUTO / nome_immagine)

            nome_audio = precedente["audio"]
            if self.rimuovi_audio_benvenuto:
                if nome_audio:
                    (CARTELLA_BENVENUTO / nome_audio).unlink(missing_ok=True)
                nome_audio = ""
            elif self.percorso_audio_benvenuto_scelto is not None:
                nome_audio_nuovo = "musica" + self.percorso_audio_benvenuto_scelto.suffix.lower()
                if nome_audio and nome_audio != nome_audio_nuovo:
                    (CARTELLA_BENVENUTO / nome_audio).unlink(missing_ok=True)
                nome_audio = nome_audio_nuovo
                shutil.copyfile(self.percorso_audio_benvenuto_scelto, CARTELLA_BENVENUTO / nome_audio)
        except OSError as errore:
            messagebox.showerror(self._t("gc_errore_titolo"), self._t("gc_errore_salvataggio_file", errore=errore))
            return

        scrivi_config_benvenuto(parser, testo_benvenuto, nome_immagine, nome_audio, self.colore_benvenuto)
        salva_parser(parser)

        self.percorso_immagine_benvenuto_scelta = None
        self.rimuovi_immagine_benvenuto = False
        self.var_nome_immagine_benvenuto.set(nome_immagine or self._t("ms_bv_nessuna_scelta"))
        self.percorso_audio_benvenuto_scelto = None
        self.rimuovi_audio_benvenuto = False
        self.var_nome_audio_benvenuto.set(nome_audio or self._t("ms_bv_nessuna_scelta"))

        messagebox.showinfo(self._t("ms_bv_salvato_titolo"), self._t("ms_bv_salvato_testo"))

    # ------------------------------------------------------------------
    # Scheda Info (pagina facoltativa raggiungibile da un link su
    # /menu e /vota-intro - stessa struttura della scheda Benvenuto, ma
    # pensata per contenuto riconsultabile: regole, sponsor, beneficenza)
    # ------------------------------------------------------------------
    def _costruisci_tab_info(self, padre):
        padding = {"padx": 8, "pady": 4}
        padre.columnconfigure(0, weight=1)

        self.label_nota_info = ttk.Label(padre, foreground="#888", justify="left", wraplength=680)
        self.label_nota_info.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self.frame_visibilita_info = ttk.LabelFrame(padre)
        self.frame_visibilita_info.grid(row=1, column=0, sticky="ew", **padding)
        self.var_info_visibile = tk.BooleanVar()
        self.checkbutton_visibile_info = ttk.Checkbutton(
            self.frame_visibilita_info, variable=self.var_info_visibile,
        )
        self.checkbutton_visibile_info.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.label_nota_visibilita_info = ttk.Label(
            self.frame_visibilita_info, foreground="#888", justify="left", wraplength=680,
        )
        self.label_nota_visibilita_info.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))

        self.frame_testo_info = ttk.LabelFrame(padre)
        self.frame_testo_info.grid(row=2, column=0, sticky="ew", **padding)
        self.testo_info_widget = tk.Text(self.frame_testo_info, height=5, width=70, wrap="word")
        self.testo_info_widget.pack(fill="x", expand=True, padx=8, pady=(8, 0))
        self.label_nota_testo_info = ttk.Label(
            self.frame_testo_info, foreground="#888", justify="left", wraplength=640,
        )
        self.label_nota_testo_info.pack(fill="x", padx=8, pady=(4, 8))

        self.frame_immagine_info = ttk.LabelFrame(padre)
        self.frame_immagine_info.grid(row=3, column=0, sticky="ew", **padding)
        self.label_campo_immagine_info = ttk.Label(self.frame_immagine_info)
        self.label_campo_immagine_info.grid(row=0, column=0, sticky="w", **padding)
        self.var_nome_immagine_info = tk.StringVar()
        ttk.Label(self.frame_immagine_info, textvariable=self.var_nome_immagine_info, width=34).grid(
            row=0, column=1, sticky="w", **padding
        )
        self.bottone_sfoglia_immagine_info = ttk.Button(
            self.frame_immagine_info, command=self._sfoglia_immagine_info,
        )
        self.bottone_sfoglia_immagine_info.grid(row=0, column=2, **padding)
        self.bottone_rimuovi_immagine_info = ttk.Button(
            self.frame_immagine_info, command=self._rimuovi_immagine_info,
        )
        self.bottone_rimuovi_immagine_info.grid(row=0, column=3, **padding)

        self.frame_audio_info = ttk.LabelFrame(padre)
        self.frame_audio_info.grid(row=4, column=0, sticky="ew", **padding)
        self.label_campo_audio_info = ttk.Label(self.frame_audio_info)
        self.label_campo_audio_info.grid(row=0, column=0, sticky="w", **padding)
        self.var_nome_audio_info = tk.StringVar()
        ttk.Label(self.frame_audio_info, textvariable=self.var_nome_audio_info, width=34).grid(
            row=0, column=1, sticky="w", **padding
        )
        self.bottone_sfoglia_audio_info = ttk.Button(
            self.frame_audio_info, command=self._sfoglia_audio_info,
        )
        self.bottone_sfoglia_audio_info.grid(row=0, column=2, **padding)
        self.bottone_rimuovi_audio_info = ttk.Button(
            self.frame_audio_info, command=self._rimuovi_audio_info,
        )
        self.bottone_rimuovi_audio_info.grid(row=0, column=3, **padding)

        self.frame_colore_info = ttk.LabelFrame(padre)
        self.frame_colore_info.grid(row=5, column=0, sticky="ew", **padding)
        self.colore_info = COLORE_INFO_DEFAULT
        self.swatch_colore_info = tk.Label(
            self.frame_colore_info, text="      ", width=8, relief="sunken", bg=self.colore_info,
        )
        self.swatch_colore_info.grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.bottone_cambia_colore_info = ttk.Button(
            self.frame_colore_info, command=self._cambia_colore_info,
        )
        self.bottone_cambia_colore_info.grid(row=0, column=1, padx=4, pady=8)
        self.label_nota_colore_info = ttk.Label(
            self.frame_colore_info, foreground="#888", justify="left", wraplength=460,
        )
        self.label_nota_colore_info.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        self.frame_donazione_info = ttk.LabelFrame(padre)
        self.frame_donazione_info.grid(row=6, column=0, sticky="ew", **padding)
        self.label_campo_link_donazione = ttk.Label(self.frame_donazione_info)
        self.label_campo_link_donazione.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_link_donazione = tk.StringVar()
        ttk.Entry(self.frame_donazione_info, textvariable=self.var_link_donazione, width=52).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 4)
        )
        self.label_nota_donazione_info = ttk.Label(
            self.frame_donazione_info, foreground="#888", justify="left", wraplength=680,
        )
        self.label_nota_donazione_info.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        self.frame_grazie_info = ttk.LabelFrame(padre)
        self.frame_grazie_info.grid(row=7, column=0, sticky="ew", **padding)
        self.testo_grazie_widget = tk.Text(self.frame_grazie_info, height=3, width=70, wrap="word")
        self.testo_grazie_widget.pack(fill="x", expand=True, padx=8, pady=(8, 0))
        self.label_nota_grazie_info = ttk.Label(
            self.frame_grazie_info, foreground="#888", justify="left", wraplength=640,
        )
        self.label_nota_grazie_info.pack(fill="x", padx=8, pady=(4, 4))
        self.bottone_anteprima_grazie_info = ttk.Button(
            self.frame_grazie_info, command=self._apri_pagina_grazie,
        )
        self.bottone_anteprima_grazie_info.pack(anchor="w", padx=8, pady=(0, 8))

        frame_bottoni_info = ttk.Frame(padre)
        frame_bottoni_info.grid(row=8, column=0, sticky="w", padx=8, pady=(4, 8))
        self.bottone_salva_info = ttk.Button(frame_bottoni_info, command=self._salva_info)
        self.bottone_salva_info.pack(side="left", padx=(0, 4))
        self.bottone_anteprima_info = ttk.Button(frame_bottoni_info, command=self._apri_pagina_info)
        self.bottone_anteprima_info.pack(side="left")

        self._carica_dati_info()

    def _carica_dati_info(self):
        parser = carica_parser()
        config = leggi_config_info(parser)

        self.var_info_visibile.set(config["visibile"])

        self.testo_info_widget.delete("1.0", "end")
        self.testo_info_widget.insert("1.0", config["testo"])

        self.var_link_donazione.set(config["link_donazione"])

        self.testo_grazie_widget.delete("1.0", "end")
        self.testo_grazie_widget.insert("1.0", config["testo_grazie"])

        self.percorso_immagine_info_scelta = None
        self.rimuovi_immagine_info = False
        self.var_nome_immagine_info.set(config["immagine"] or self._t("ms_bv_nessuna_scelta"))

        self.percorso_audio_info_scelto = None
        self.rimuovi_audio_info = False
        self.var_nome_audio_info.set(config["audio"] or self._t("ms_bv_nessuna_scelta"))

        self._imposta_colore_info(config["colore"])

    def _sfoglia_immagine_info(self):
        percorso = filedialog.askopenfilename(
            title=self._t("ms_inf_scegli_immagine_titolo"),
            filetypes=[
                (self._t("ms_inf_filtro_immagini"), "*.jpg *.jpeg *.png *.gif"),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if not percorso:
            return
        self.percorso_immagine_info_scelta = Path(percorso)
        self.rimuovi_immagine_info = False
        self.var_nome_immagine_info.set(self.percorso_immagine_info_scelta.name)
        self._prova_estrai_colore_da_immagine_info(self.percorso_immagine_info_scelta)

    def _rimuovi_immagine_info(self):
        self.percorso_immagine_info_scelta = None
        self.rimuovi_immagine_info = True
        self.var_nome_immagine_info.set(self._t("ms_bv_nessuna_scelta"))

    def _prova_estrai_colore_da_immagine_info(self, percorso: Path):
        """Stessa proposta automatica di colore della scheda Benvenuto
        (vedi _prova_estrai_colore_da_immagine), duplicata qui per la
        scheda Info - Pillow resta opzionale, non blocca il resto."""
        try:
            from PIL import Image
        except ImportError:
            vuole_installare = messagebox.askyesno(
                self._t("ms_bv_pillow_mancante_titolo"), self._t("ms_bv_pillow_mancante_testo"),
            )
            if vuole_installare:
                risultato = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pillow"], capture_output=True, text=True,
                )
                if risultato.returncode == 0:
                    messagebox.showinfo(self._t("gc_installazione_riuscita_titolo"), self._t("ms_bv_pillow_installata_testo"))
                else:
                    messagebox.showerror(
                        self._t("gc_installazione_fallita_titolo"),
                        self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
                    )
            return

        try:
            with Image.open(percorso) as immagine:
                r, g, b = immagine.convert("RGB").resize((1, 1)).getpixel((0, 0))
            self._imposta_colore_info(f"#{r:02x}{g:02x}{b:02x}")
        except Exception:
            pass

    def _imposta_colore_info(self, colore_hex: str):
        self.colore_info = colore_hex
        self.swatch_colore_info.configure(bg=colore_hex)

    def _cambia_colore_info(self):
        scelto = colorchooser.askcolor(color=self.colore_info, title=self._t("ms_inf_bottone_cambia_colore"))
        if scelto and scelto[1]:
            self._imposta_colore_info(scelto[1])

    def _sfoglia_audio_info(self):
        percorso = filedialog.askopenfilename(
            title=self._t("ms_inf_scegli_audio_titolo"),
            filetypes=[
                (self._t("ms_inf_filtro_audio"), "*.mp3"),
                (self._t("gc_filtro_tutti"), "*.*"),
            ],
        )
        if not percorso:
            return
        self.percorso_audio_info_scelto = Path(percorso)
        self.rimuovi_audio_info = False
        self.var_nome_audio_info.set(self.percorso_audio_info_scelto.name)

    def _rimuovi_audio_info(self):
        self.percorso_audio_info_scelto = None
        self.rimuovi_audio_info = True
        self.var_nome_audio_info.set(self._t("ms_bv_nessuna_scelta"))

    def _salva_info(self):
        testo_info = self.testo_info_widget.get("1.0", "end-1c")
        testo_grazie = self.testo_grazie_widget.get("1.0", "end-1c")
        parser = carica_parser()
        precedente = leggi_config_info(parser)

        try:
            CARTELLA_INFO.mkdir(parents=True, exist_ok=True)

            nome_immagine = precedente["immagine"]
            if self.rimuovi_immagine_info:
                if nome_immagine:
                    (CARTELLA_INFO / nome_immagine).unlink(missing_ok=True)
                nome_immagine = ""
            elif self.percorso_immagine_info_scelta is not None:
                nome_immagine_nuovo = "immagine" + self.percorso_immagine_info_scelta.suffix.lower()
                if nome_immagine and nome_immagine != nome_immagine_nuovo:
                    (CARTELLA_INFO / nome_immagine).unlink(missing_ok=True)
                nome_immagine = nome_immagine_nuovo
                shutil.copyfile(self.percorso_immagine_info_scelta, CARTELLA_INFO / nome_immagine)

            nome_audio = precedente["audio"]
            if self.rimuovi_audio_info:
                if nome_audio:
                    (CARTELLA_INFO / nome_audio).unlink(missing_ok=True)
                nome_audio = ""
            elif self.percorso_audio_info_scelto is not None:
                nome_audio_nuovo = "audio" + self.percorso_audio_info_scelto.suffix.lower()
                if nome_audio and nome_audio != nome_audio_nuovo:
                    (CARTELLA_INFO / nome_audio).unlink(missing_ok=True)
                nome_audio = nome_audio_nuovo
                shutil.copyfile(self.percorso_audio_info_scelto, CARTELLA_INFO / nome_audio)
        except OSError as errore:
            messagebox.showerror(self._t("gc_errore_titolo"), self._t("gc_errore_salvataggio_file", errore=errore))
            return

        scrivi_config_info(
            parser, testo_info, nome_immagine, nome_audio, self.colore_info,
            self.var_info_visibile.get(), self.var_link_donazione.get().strip(), testo_grazie,
        )
        salva_parser(parser)

        self.percorso_immagine_info_scelta = None
        self.rimuovi_immagine_info = False
        self.var_nome_immagine_info.set(nome_immagine or self._t("ms_bv_nessuna_scelta"))
        self.percorso_audio_info_scelto = None
        self.rimuovi_audio_info = False
        self.var_nome_audio_info.set(nome_audio or self._t("ms_bv_nessuna_scelta"))

        messagebox.showinfo(self._t("ms_inf_salvato_titolo"), self._t("ms_inf_salvato_testo"))

    def _apri_pagina_info(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        webbrowser.open(f"http://localhost:{porta}/info")

    def _apri_pagina_grazie(self):
        parser = carica_parser()
        porta = parser.getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)
        webbrowser.open(f"http://localhost:{porta}/grazie")

    # ------------------------------------------------------------------
    # Scheda QR Code (genera due QR pronti da stampare: uno per collegarsi
    # al WiFi ospiti, uno per aprire la pagina di voto - tutto generato in
    # locale, nessun sito esterno coinvolto)
    # ------------------------------------------------------------------
    def _costruisci_tab_qrcode(self, padre):
        padding = {"padx": 8, "pady": 4}
        padre.columnconfigure(0, weight=1)

        self.label_nota_qr = ttk.Label(padre, foreground="#888", justify="left", wraplength=680)
        self.label_nota_qr.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self.frame_qr_wifi = ttk.LabelFrame(padre)
        self.frame_qr_wifi.grid(row=1, column=0, sticky="ew", **padding)
        self.frame_qr_wifi.columnconfigure(2, weight=1)
        self.bottone_qr_salva_profilo = ttk.Button(self.frame_qr_wifi, command=self._salva_profilo_qr)
        self.bottone_qr_salva_profilo.grid(row=0, column=2, sticky="w", padx=8, pady=(8, 4))
        self.label_qr_campo_ssid = ttk.Label(self.frame_qr_wifi)
        self.label_qr_campo_ssid.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_qr_ssid = tk.StringVar()
        ttk.Entry(self.frame_qr_wifi, textvariable=self.var_qr_ssid, width=32).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 4)
        )
        self.label_qr_campo_password = ttk.Label(self.frame_qr_wifi)
        self.label_qr_campo_password.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.var_qr_password = tk.StringVar()
        ttk.Entry(self.frame_qr_wifi, textvariable=self.var_qr_password, width=32).grid(
            row=1, column=1, sticky="w", padx=(0, 8), pady=4
        )
        self.label_qr_campo_sicurezza = ttk.Label(self.frame_qr_wifi)
        self.label_qr_campo_sicurezza.grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
        self.var_qr_sicurezza_display = tk.StringVar()
        self.combo_qr_sicurezza = ttk.Combobox(
            self.frame_qr_wifi, textvariable=self.var_qr_sicurezza_display, state="readonly", width=20, values=(),
        )
        self.combo_qr_sicurezza.grid(row=2, column=1, sticky="w", padx=(0, 8), pady=(4, 8))

        self.frame_qr_indirizzo = ttk.LabelFrame(padre)
        self.frame_qr_indirizzo.grid(row=2, column=0, sticky="ew", **padding)
        self.label_qr_campo_ip = ttk.Label(self.frame_qr_indirizzo)
        self.label_qr_campo_ip.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_qr_ip = tk.StringVar()
        ttk.Entry(self.frame_qr_indirizzo, textvariable=self.var_qr_ip, width=20).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 4)
        )
        self.bottone_qr_rileva_ip = ttk.Button(
            self.frame_qr_indirizzo, command=lambda: self.var_qr_ip.set(self._rileva_ip_locale()),
        )
        self.bottone_qr_rileva_ip.grid(row=0, column=2, padx=(0, 8), pady=(8, 4))
        self.label_nota_qr_ip = ttk.Label(
            self.frame_qr_indirizzo, foreground="#888", justify="left", wraplength=640,
        )
        self.label_nota_qr_ip.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        self.bottone_qr_genera = ttk.Button(padre, command=self._genera_qrcode)
        self.bottone_qr_genera.grid(row=3, column=0, sticky="w", padx=8, pady=(4, 8))

        frame_immagini = ttk.Frame(padre)
        frame_immagini.grid(row=4, column=0, sticky="w", padx=8, pady=(0, 8))

        colonna_wifi = ttk.Frame(frame_immagini)
        colonna_wifi.pack(side="left", padx=(0, 20))
        self.label_qr_etichetta_wifi = ttk.Label(colonna_wifi)
        self.label_qr_etichetta_wifi.pack()
        riquadro_qr_wifi = tk.Frame(colonna_wifi, width=220, height=220, relief="groove", borderwidth=1)
        riquadro_qr_wifi.pack(pady=4)
        riquadro_qr_wifi.pack_propagate(False)
        self.label_qr_wifi_immagine = ttk.Label(riquadro_qr_wifi, anchor="center")
        self.label_qr_wifi_immagine.pack(fill="both", expand=True)
        self.bottone_qr_salva_wifi = ttk.Button(colonna_wifi, command=self._salva_immagine_wifi, state="disabled")
        self.bottone_qr_salva_wifi.pack()

        colonna_voto = ttk.Frame(frame_immagini)
        colonna_voto.pack(side="left")
        self.label_qr_etichetta_voto = ttk.Label(colonna_voto)
        self.label_qr_etichetta_voto.pack()
        riquadro_qr_voto = tk.Frame(colonna_voto, width=220, height=220, relief="groove", borderwidth=1)
        riquadro_qr_voto.pack(pady=4)
        riquadro_qr_voto.pack_propagate(False)
        self.label_qr_voto_immagine = ttk.Label(riquadro_qr_voto, anchor="center")
        self.label_qr_voto_immagine.pack(fill="both", expand=True)
        self.bottone_qr_salva_voto = ttk.Button(colonna_voto, command=self._salva_immagine_voto, state="disabled")
        self.bottone_qr_salva_voto.pack()

        self.qr_wifi_immagine_pil = None
        self.qr_voto_immagine_pil = None

        self.frame_qr_cartello = ttk.LabelFrame(padre)
        self.frame_qr_cartello.grid(row=5, column=0, sticky="ew", **padding)
        self.frame_qr_cartello.columnconfigure(1, weight=1)

        self.label_qr_cartello_titolo = ttk.Label(self.frame_qr_cartello)
        self.label_qr_cartello_titolo.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.var_qr_cartello_titolo = tk.StringVar()
        ttk.Entry(self.frame_qr_cartello, textvariable=self.var_qr_cartello_titolo, width=40).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 4)
        )

        self.label_qr_cartello_sottotitolo = ttk.Label(self.frame_qr_cartello)
        self.label_qr_cartello_sottotitolo.grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.var_qr_cartello_sottotitolo = tk.StringVar()
        ttk.Entry(self.frame_qr_cartello, textvariable=self.var_qr_cartello_sottotitolo, width=40).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=4
        )

        self.var_qr_cartello_includi_voto = tk.BooleanVar(value=True)
        self.checkbutton_qr_cartello_includi_voto = ttk.Checkbutton(
            self.frame_qr_cartello, variable=self.var_qr_cartello_includi_voto,
        )
        self.checkbutton_qr_cartello_includi_voto.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        self._percorso_ultimo_cartello = None

        frame_bottoni_cartello = ttk.Frame(self.frame_qr_cartello)
        frame_bottoni_cartello.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 8))
        self.bottone_qr_genera_cartello = ttk.Button(frame_bottoni_cartello, command=self._genera_cartello_stampabile)
        self.bottone_qr_genera_cartello.pack(side="left", padx=(0, 4))
        self.bottone_qr_stampa_cartello = ttk.Button(frame_bottoni_cartello, command=self._stampa_cartello)
        self.bottone_qr_stampa_cartello.pack(side="left")

        self.frame_qr_profili = ttk.LabelFrame(padre)
        self.frame_qr_profili.grid(row=6, column=0, sticky="ew", **padding)
        self.frame_qr_profili.columnconfigure(0, weight=1)

        colonne_profili = ("nome", "ssid", "ip")
        self.albero_qr_profili = ttk.Treeview(
            self.frame_qr_profili, columns=colonne_profili, show="headings", height=5, selectmode="browse",
        )
        for colonna, larghezza in (("nome", 200), ("ssid", 200), ("ip", 140)):
            self.albero_qr_profili.column(colonna, width=larghezza)
        self.albero_qr_profili.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        frame_bottoni_profili = ttk.Frame(self.frame_qr_profili)
        frame_bottoni_profili.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))
        self.bottone_qr_carica_profilo = ttk.Button(frame_bottoni_profili, command=self._carica_profilo_qr_selezionato)
        self.bottone_qr_carica_profilo.pack(side="left", padx=(0, 4))
        self.bottone_qr_elimina_profilo = ttk.Button(frame_bottoni_profili, command=self._elimina_profilo_qr_selezionato)
        self.bottone_qr_elimina_profilo.pack(side="left")

        self._carica_dati_qrcode()
        self._aggiorna_lista_profili_qr()

    QR_SICUREZZA_CHIAVI = ("WPA", "WEP", "nopass")
    QR_SICUREZZA_CHIAVE_TRADUZIONE = {
        "WPA": "ms_qr_sicurezza_wpa",
        "WEP": "ms_qr_sicurezza_wep",
        "nopass": "ms_qr_sicurezza_nessuna",
    }

    def _etichette_qr_sicurezza(self) -> list:
        return [self._t(self.QR_SICUREZZA_CHIAVE_TRADUZIONE[chiave]) for chiave in self.QR_SICUREZZA_CHIAVI]

    def _chiave_da_etichetta_qr_sicurezza(self, etichetta: str) -> str:
        for chiave in self.QR_SICUREZZA_CHIAVI:
            if self._t(self.QR_SICUREZZA_CHIAVE_TRADUZIONE[chiave]) == etichetta:
                return chiave
        return "WPA"

    @staticmethod
    def _rileva_ip_locale() -> str:
        """Trucco standard per scoprire l'IP locale usato per uscire sulla
        rete, senza mandare davvero traffico: connect() su UDP si limita a
        far calcolare al sistema operativo la rotta/interfaccia giusta,
        nessun pacchetto parte verso 8.8.8.8. Se fallisce (nessuna rete),
        ripiega su 127.0.0.1 - meglio un valore ovviamente sbagliato che
        un crash, l'utente puo' sempre correggerlo a mano."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    @staticmethod
    def _escape_wifi_qr(valore: str) -> str:
        """Escape dei caratteri speciali per il formato standard WIFI:...
        letto dalle fotocamere dei telefoni (specifica ZXing)."""
        for carattere in ("\\", ";", ",", ":", '"'):
            valore = valore.replace(carattere, "\\" + carattere)
        return valore

    def _crea_immagine_qr(self, dati: str):
        import qrcode
        generatore = qrcode.QRCode(box_size=10, border=4)
        generatore.add_data(dati)
        generatore.make(fit=True)
        return generatore.make_image(fill_color="black", back_color="white").get_image()

    def _carica_dati_qrcode(self):
        parser = carica_parser()
        self.var_qr_ssid.set(parser.get(SEZIONE_QRCODE, "Ssid", fallback=""))
        self.var_qr_password.set(parser.get(SEZIONE_QRCODE, "Password", fallback=""))
        sicurezza = parser.get(SEZIONE_QRCODE, "Sicurezza", fallback="WPA")
        if sicurezza not in self.QR_SICUREZZA_CHIAVI:
            sicurezza = "WPA"
        self.combo_qr_sicurezza.configure(values=self._etichette_qr_sicurezza())
        self.var_qr_sicurezza_display.set(self._t(self.QR_SICUREZZA_CHIAVE_TRADUZIONE[sicurezza]))
        ip_salvato = parser.get(SEZIONE_QRCODE, "Ip", fallback="").strip()
        self.var_qr_ip.set(ip_salvato or self._rileva_ip_locale())
        self.var_qr_cartello_titolo.set(
            parser.get(SEZIONE_QRCODE, "Cartello_Titolo", fallback="").strip() or self.var_qr_ssid.get()
        )
        self.var_qr_cartello_sottotitolo.set(
            parser.get(SEZIONE_QRCODE, "Cartello_Sottotitolo", fallback="").strip()
            or self._t("ms_qr_cartello_sottotitolo_default")
        )
        self.var_qr_cartello_includi_voto.set(
            parser.getboolean(SEZIONE_QRCODE, "Cartello_Includi_Voto", fallback=True)
        )

    @staticmethod
    def _leggi_profili_qr() -> list:
        if not FILE_QR_PROFILI.is_file():
            return []
        try:
            with open(FILE_QR_PROFILI, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _scrivi_profili_qr(profili: list):
        CARTELLA_QR_PROFILI.mkdir(parents=True, exist_ok=True)
        with open(FILE_QR_PROFILI, "w", encoding="utf-8") as file:
            json.dump(profili, file, indent=2, ensure_ascii=False)

    @staticmethod
    def _nome_cartella_sicuro(nome: str) -> str:
        pulito = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in nome).strip()
        return pulito.replace(" ", "_") or "profilo"

    def _aggiorna_lista_profili_qr(self):
        for riga in self.albero_qr_profili.get_children():
            self.albero_qr_profili.delete(riga)
        for profilo in self._leggi_profili_qr():
            self.albero_qr_profili.insert(
                "", "end", iid=profilo["nome"],
                values=(profilo["nome"], profilo.get("ssid", ""), profilo.get("ip", "")),
            )

    def _salva_profilo_qr(self):
        nome = self.var_qr_ssid.get().strip()
        if not nome:
            messagebox.showerror(self._t("ms_qr_errore_ssid_titolo"), self._t("ms_qr_errore_ssid_testo"))
            return
        if self.qr_wifi_immagine_pil is None or self.qr_voto_immagine_pil is None:
            messagebox.showerror(self._t("ms_qr_errore_genera_prima_titolo"), self._t("ms_qr_errore_genera_prima_testo"))
            return

        profili = self._leggi_profili_qr()
        esiste_gia = any(p["nome"] == nome for p in profili)
        if esiste_gia:
            if not messagebox.askyesno(self._t("ms_qr_profilo_esiste_titolo"), self._t("ms_qr_profilo_esiste_testo", nome=nome)):
                return
            profili = [p for p in profili if p["nome"] != nome]

        cartella_profilo = CARTELLA_QR_PROFILI / self._nome_cartella_sicuro(nome)
        cartella_profilo.mkdir(parents=True, exist_ok=True)
        percorso_wifi = cartella_profilo / "qr_wifi.png"
        percorso_voto = cartella_profilo / "qr_voto.png"
        self.qr_wifi_immagine_pil.save(percorso_wifi)
        self.qr_voto_immagine_pil.save(percorso_voto)

        titolo_cartello = self.var_qr_cartello_titolo.get().strip() or nome
        sottotitolo_cartello = self.var_qr_cartello_sottotitolo.get().strip()
        includi_voto_cartello = self.var_qr_cartello_includi_voto.get()
        cartello = self._crea_cartello_qr(
            titolo_cartello, sottotitolo_cartello, self.var_qr_ssid.get().strip(),
            self.var_qr_password.get(), includi_voto_cartello,
        )
        percorso_cartello = cartella_profilo / "cartello.png"
        cartello.save(percorso_cartello, dpi=(150, 150))

        profili.append({
            "nome": nome,
            "ssid": self.var_qr_ssid.get().strip(),
            "password": self.var_qr_password.get(),
            "sicurezza": self._chiave_da_etichetta_qr_sicurezza(self.var_qr_sicurezza_display.get()),
            "ip": self.var_qr_ip.get().strip(),
            "cartello_titolo": titolo_cartello,
            "cartello_sottotitolo": sottotitolo_cartello,
            "cartello_includi_voto": includi_voto_cartello,
            "file_wifi": str(percorso_wifi),
            "file_voto": str(percorso_voto),
            "file_cartello": str(percorso_cartello),
            "data": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        self._scrivi_profili_qr(profili)
        self._aggiorna_lista_profili_qr()
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_qr_profilo_salvato_testo", nome=nome))

    def _profilo_qr_selezionato(self):
        selezione = self.albero_qr_profili.selection()
        if not selezione:
            messagebox.showinfo(self._t("ms_qr_nessuna_selezione_titolo"), self._t("ms_qr_nessuna_selezione_testo"))
            return None
        nome = selezione[0]
        for profilo in self._leggi_profili_qr():
            if profilo["nome"] == nome:
                return profilo
        return None

    def _carica_profilo_qr_selezionato(self):
        profilo = self._profilo_qr_selezionato()
        if profilo is None:
            return

        self.var_qr_ssid.set(profilo.get("ssid", ""))
        self.var_qr_password.set(profilo.get("password", ""))
        sicurezza = profilo.get("sicurezza", "WPA")
        if sicurezza not in self.QR_SICUREZZA_CHIAVI:
            sicurezza = "WPA"
        self.var_qr_sicurezza_display.set(self._t(self.QR_SICUREZZA_CHIAVE_TRADUZIONE[sicurezza]))
        self.var_qr_ip.set(profilo.get("ip", ""))
        self.var_qr_cartello_titolo.set(profilo.get("cartello_titolo", profilo.get("nome", "")))
        self.var_qr_cartello_sottotitolo.set(profilo.get("cartello_sottotitolo", ""))
        self.var_qr_cartello_includi_voto.set(profilo.get("cartello_includi_voto", True))

        from PIL import Image, ImageTk
        try:
            self.qr_wifi_immagine_pil = Image.open(profilo["file_wifi"])
            self.qr_voto_immagine_pil = Image.open(profilo["file_voto"])
        except (OSError, KeyError):
            messagebox.showerror(self._t("ms_qr_errore_file_mancante_titolo"), self._t("ms_qr_errore_file_mancante_testo"))
            return

        self.qr_wifi_photoimage = ImageTk.PhotoImage(self.qr_wifi_immagine_pil.resize((220, 220)))
        self.qr_voto_photoimage = ImageTk.PhotoImage(self.qr_voto_immagine_pil.resize((220, 220)))
        self.label_qr_wifi_immagine.configure(image=self.qr_wifi_photoimage)
        self.label_qr_voto_immagine.configure(image=self.qr_voto_photoimage)
        self.bottone_qr_salva_wifi.configure(state="normal")
        self.bottone_qr_salva_voto.configure(state="normal")

        file_cartello = profilo.get("file_cartello")
        if file_cartello and Path(file_cartello).is_file():
            self._percorso_ultimo_cartello = file_cartello

    def _elimina_profilo_qr_selezionato(self):
        profilo = self._profilo_qr_selezionato()
        if profilo is None:
            return
        if not messagebox.askyesno(
            self._t("ms_qr_conferma_elimina_titolo"), self._t("ms_qr_conferma_elimina_testo", nome=profilo["nome"]),
        ):
            return

        cartella_profilo = CARTELLA_QR_PROFILI / self._nome_cartella_sicuro(profilo["nome"])
        shutil.rmtree(cartella_profilo, ignore_errors=True)

        profili = [p for p in self._leggi_profili_qr() if p["nome"] != profilo["nome"]]
        self._scrivi_profili_qr(profili)
        self._aggiorna_lista_profili_qr()

    def _salva_dati_qrcode(self):
        parser = carica_parser()
        parser[SEZIONE_QRCODE]["Ssid"] = self.var_qr_ssid.get().strip()
        parser[SEZIONE_QRCODE]["Password"] = self.var_qr_password.get()
        parser[SEZIONE_QRCODE]["Sicurezza"] = self._chiave_da_etichetta_qr_sicurezza(self.var_qr_sicurezza_display.get())
        parser[SEZIONE_QRCODE]["Ip"] = self.var_qr_ip.get().strip()
        parser[SEZIONE_QRCODE]["Cartello_Titolo"] = self.var_qr_cartello_titolo.get().strip()
        parser[SEZIONE_QRCODE]["Cartello_Sottotitolo"] = self.var_qr_cartello_sottotitolo.get().strip()
        parser[SEZIONE_QRCODE]["Cartello_Includi_Voto"] = str(self.var_qr_cartello_includi_voto.get())
        salva_parser(parser)

    def _genera_qrcode(self):
        ssid = self.var_qr_ssid.get().strip()
        if not ssid:
            messagebox.showerror(self._t("ms_qr_errore_ssid_titolo"), self._t("ms_qr_errore_ssid_testo"))
            return
        ip = self.var_qr_ip.get().strip()
        if not ip:
            messagebox.showerror(self._t("ms_qr_errore_ip_titolo"), self._t("ms_qr_errore_ip_testo"))
            return

        try:
            import qrcode  # noqa: F401  (verifica solo che sia installato)
            from PIL import ImageTk
        except ImportError:
            vuole_installare = messagebox.askyesno(
                self._t("ms_qr_dipendenza_mancante_titolo"), self._t("ms_qr_dipendenza_mancante_testo"),
            )
            if vuole_installare:
                risultato = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "qrcode", "pillow"], capture_output=True, text=True,
                )
                if risultato.returncode == 0:
                    messagebox.showinfo(
                        self._t("gc_installazione_riuscita_titolo"), self._t("ms_qr_dipendenza_installata_testo"),
                    )
                else:
                    messagebox.showerror(
                        self._t("gc_installazione_fallita_titolo"),
                        self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
                    )
            return

        password = self.var_qr_password.get()
        chiave_sicurezza = self._chiave_da_etichetta_qr_sicurezza(self.var_qr_sicurezza_display.get())
        porta = carica_parser().getint(SEZIONE_VOTO, "Porta_Server", fallback=8080)

        if chiave_sicurezza == "nopass":
            testo_wifi = f"WIFI:T:nopass;S:{self._escape_wifi_qr(ssid)};;"
        else:
            testo_wifi = f"WIFI:T:{chiave_sicurezza};S:{self._escape_wifi_qr(ssid)};P:{self._escape_wifi_qr(password)};;"
        testo_voto = f"http://{ip}:{porta}/"

        self.qr_wifi_immagine_pil = self._crea_immagine_qr(testo_wifi)
        self.qr_voto_immagine_pil = self._crea_immagine_qr(testo_voto)

        self.qr_wifi_photoimage = ImageTk.PhotoImage(self.qr_wifi_immagine_pil.resize((220, 220)))
        self.qr_voto_photoimage = ImageTk.PhotoImage(self.qr_voto_immagine_pil.resize((220, 220)))
        self.label_qr_wifi_immagine.configure(image=self.qr_wifi_photoimage)
        self.label_qr_voto_immagine.configure(image=self.qr_voto_photoimage)
        self.bottone_qr_salva_wifi.configure(state="normal")
        self.bottone_qr_salva_voto.configure(state="normal")

        self._salva_dati_qrcode()

    def _salva_immagine_wifi(self):
        self._salva_immagine_qr(self.qr_wifi_immagine_pil, "qr_wifi.png")

    def _salva_immagine_voto(self):
        self._salva_immagine_qr(self.qr_voto_immagine_pil, "qr_voto.png")

    def _salva_immagine_qr(self, immagine, nome_suggerito: str):
        if immagine is None:
            return
        percorso = filedialog.asksaveasfilename(
            title=self._t("ms_qr_salva_titolo"), defaultextension=".png", initialfile=nome_suggerito,
            filetypes=[("PNG", "*.png")],
        )
        if not percorso:
            return
        immagine.save(percorso)
        messagebox.showinfo(self._t("ms_qr_salva_titolo"), self._t("ms_qr_salvato_testo", percorso=percorso))

    @staticmethod
    def _carica_font_cartello(dimensione: int, grassetto: bool = False):
        """Font di sistema per il cartello stampabile - percorso esplicito
        nella cartella Fonts di Windows (ImageFont.truetype non cerca lì
        da solo se non gli si passa un percorso assoluto). Se non trovato
        ripiega sul font bitmap di default di Pillow: meno bello ma non
        fa mai fallire la generazione."""
        from PIL import ImageFont
        cartella_font = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        nomi = (["arialbd.ttf"] if grassetto else []) + ["arial.ttf"]
        for nome in nomi:
            percorso_font = cartella_font / nome
            if percorso_font.is_file():
                try:
                    return ImageFont.truetype(str(percorso_font), dimensione)
                except OSError:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _ritaglia_bordo_qr(immagine):
        """Il QR generato ha un bordo bianco 'quiet zone' incorporato
        nell'immagine (richiesto dallo standard, utile perche' le
        fotocamere lo leggano bene da soli) - ma per il cartello, dove
        c'e' gia' testo intorno, e' solo spazio vuoto in piu' da
        togliere per avvicinare il testo al QR."""
        from PIL import ImageOps
        bbox = ImageOps.invert(immagine.convert("L")).getbbox()
        return immagine.crop(bbox) if bbox else immagine

    def _crea_cartello_qr(self, titolo: str, sottotitolo: str, ssid: str, password: str, includi_voto: bool):
        from PIL import Image, ImageDraw

        larghezza = 1240
        dimensione_qr = 480
        # Altezza generosa, si ritaglia alla fine in base a dove finisce
        # davvero il contenuto (vedi crop finale) - cosi' non si rischia
        # piu' di tagliare via il secondo QR per un calcolo sbagliato.
        altezza_massima = 2000

        cartello = Image.new("RGB", (larghezza, altezza_massima), "white")
        disegno = ImageDraw.Draw(cartello)

        font_titolo = self._carica_font_cartello(52, grassetto=True)
        font_sottotitolo = self._carica_font_cartello(28)
        font_passo = self._carica_font_cartello(34, grassetto=True)
        font_testo = self._carica_font_cartello(28)
        font_nota = self._carica_font_cartello(22)

        def centra_testo(y, testo, font, riempimento="black"):
            larghezza_testo = disegno.textlength(testo, font=font)
            disegno.text(((larghezza - larghezza_testo) / 2, y), testo, font=font, fill=riempimento)
            return y

        y = 40
        centra_testo(y, titolo, font_titolo)
        y += 66
        if sottotitolo:
            centra_testo(y, sottotitolo, font_sottotitolo, riempimento="#444")
            y += 50
        else:
            y += 10

        centra_testo(y, self._t("ms_qr_cartello_passo1"), font_passo)
        y += 50
        qr_wifi = self._ritaglia_bordo_qr(self.qr_wifi_immagine_pil).resize((dimensione_qr, dimensione_qr))
        cartello.paste(qr_wifi, ((larghezza - dimensione_qr) // 2, y))
        y += dimensione_qr + 4
        centra_testo(y, self._t("ms_qr_cartello_rete", rete=ssid), font_testo)
        y += 36
        centra_testo(y, self._t("ms_qr_cartello_password", password=password), font_testo)
        y += 36
        centra_testo(y, self._t("ms_qr_cartello_nota_wifi"), font_nota, riempimento="#555")
        y += 90

        if includi_voto:
            centra_testo(y, self._t("ms_qr_cartello_passo2"), font_passo)
            y += 50
            qr_voto = self._ritaglia_bordo_qr(self.qr_voto_immagine_pil).resize((dimensione_qr, dimensione_qr))
            cartello.paste(qr_voto, ((larghezza - dimensione_qr) // 2, y))
            y += dimensione_qr + 4
            centra_testo(y, self._t("ms_qr_cartello_nota_voto"), font_nota, riempimento="#555")
            y += 30

        return cartello.crop((0, 0, larghezza, min(y + 40, altezza_massima)))

    def _genera_cartello_stampabile(self):
        includi_voto = self.var_qr_cartello_includi_voto.get()
        if self.qr_wifi_immagine_pil is None or (includi_voto and self.qr_voto_immagine_pil is None):
            messagebox.showerror(self._t("ms_qr_errore_genera_prima_titolo"), self._t("ms_qr_errore_genera_prima_testo"))
            return

        titolo = self.var_qr_cartello_titolo.get().strip() or self.var_qr_ssid.get().strip()
        sottotitolo = self.var_qr_cartello_sottotitolo.get().strip()
        cartello = self._crea_cartello_qr(
            titolo, sottotitolo, self.var_qr_ssid.get().strip(), self.var_qr_password.get(), includi_voto,
        )

        self._salva_dati_qrcode()

        # Anteprima prima di chiedere dove salvare: si apre con il
        # visualizzatore immagini di Windows, cosi' si controlla il
        # risultato prima di scegliere la posizione definitiva.
        percorso_anteprima = Path(tempfile.gettempdir()) / f"anteprima_cartello_{secrets.token_hex(4)}.png"
        cartello.save(percorso_anteprima, dpi=(150, 150))
        self._percorso_ultimo_cartello = percorso_anteprima
        try:
            os.startfile(percorso_anteprima)
        except OSError:
            pass

        if not messagebox.askyesno(self._t("ms_qr_cartello_anteprima_titolo"), self._t("ms_qr_cartello_anteprima_testo")):
            return

        nome_suggerito = f"cartello_{self._nome_cartella_sicuro(self.var_qr_ssid.get().strip())}.png"
        percorso = filedialog.asksaveasfilename(
            title=self._t("ms_qr_salva_titolo"), defaultextension=".png", initialfile=nome_suggerito,
            filetypes=[("PNG", "*.png")],
        )
        if not percorso:
            return
        cartello.save(percorso, dpi=(150, 150))
        self._percorso_ultimo_cartello = percorso
        messagebox.showinfo(self._t("ms_qr_salva_titolo"), self._t("ms_qr_cartello_salvato_testo", percorso=percorso))

    def _stampa_cartello(self):
        """Manda il cartello direttamente alla stampante predefinita via
        GDI (win32print/win32ui), invece del verbo di stampa diretto di
        Windows: quello richiama una vecchia finestra di sistema
        (shimgvw.dll) nota per dare "errore interno" in modo
        intermittente. Qui non passiamo da nessuna finestra esterna
        fragile - solo la vera API di stampa di Windows."""
        if not self._percorso_ultimo_cartello or not Path(self._percorso_ultimo_cartello).is_file():
            messagebox.showerror(self._t("ms_qr_errore_genera_prima_titolo"), self._t("ms_qr_errore_genera_prima_testo"))
            return

        try:
            import win32print
            import win32ui
            import win32con
            from PIL import Image, ImageWin
        except ImportError:
            vuole_installare = messagebox.askyesno(
                self._t("ms_qr_dipendenza_mancante_titolo"), self._t("ms_qr_dipendenza_stampa_mancante_testo"),
            )
            if vuole_installare:
                risultato = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "pywin32"], capture_output=True, text=True,
                )
                if risultato.returncode == 0:
                    messagebox.showinfo(
                        self._t("gc_installazione_riuscita_titolo"), self._t("ms_qr_dipendenza_installata_testo"),
                    )
                else:
                    messagebox.showerror(
                        self._t("gc_installazione_fallita_titolo"),
                        self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
                    )
            return

        try:
            nome_stampante = win32print.GetDefaultPrinter()
        except Exception as errore:  # noqa: BLE001 - qualunque problema qui, meglio un messaggio chiaro che un crash
            messagebox.showerror(self._t("gc_errore_titolo"), str(errore))
            return

        # Anteprima visiva prima di chiedere conferma: utile sia dopo
        # "Genera" (per ricontrollare) sia quando si carica un profilo
        # gia' pronto (dove non si e' appena visto nulla).
        try:
            os.startfile(self._percorso_ultimo_cartello)
        except OSError:
            pass

        if not messagebox.askyesno(
            self._t("ms_qr_conferma_stampa_titolo"), self._t("ms_qr_conferma_stampa_testo", stampante=nome_stampante),
        ):
            return

        dc_stampante = None
        try:
            immagine = Image.open(self._percorso_ultimo_cartello).convert("RGB")

            dc_stampante = win32ui.CreateDC()
            dc_stampante.CreatePrinterDC(nome_stampante)

            larghezza_pagina = dc_stampante.GetDeviceCaps(win32con.HORZRES)
            altezza_pagina = dc_stampante.GetDeviceCaps(win32con.VERTRES)
            scala = min(larghezza_pagina / immagine.width, altezza_pagina / immagine.height)
            larghezza_stampa = int(immagine.width * scala)
            altezza_stampa = int(immagine.height * scala)
            x = (larghezza_pagina - larghezza_stampa) // 2
            y = (altezza_pagina - altezza_stampa) // 2

            dc_stampante.StartDoc(self._t("ms_qr_titolo_cartello"))
            dc_stampante.StartPage()
            ImageWin.Dib(immagine).draw(dc_stampante.GetHandleOutput(), (x, y, x + larghezza_stampa, y + altezza_stampa))
            dc_stampante.EndPage()
            dc_stampante.EndDoc()
        except Exception as errore:  # noqa: BLE001 - stampanti/driver diversi possono fallire in tanti modi diversi
            messagebox.showerror(self._t("gc_errore_titolo"), str(errore))
            return
        finally:
            if dc_stampante is not None:
                dc_stampante.DeleteDC()

        messagebox.showinfo(self._t("ms_qr_stampa_titolo"), self._t("ms_qr_stampa_inviata_testo", stampante=nome_stampante))

    # ------------------------------------------------------------------
    # Scheda Storico (database SQLite scritto da VotoShow.py: voti e
    # sequenze mandate in onda nel tempo, su piu' stagioni)
    # ------------------------------------------------------------------
    def _costruisci_tab_storico(self, padre):
        self._mappa_giorni_storico = {}  # etichetta (col nome del giorno) -> data ISO, ricostruita ad ogni aggiornamento
        padding = {"padx": 8, "pady": 4}
        padre.columnconfigure(0, weight=1)
        padre.rowconfigure(3, weight=1)

        self.label_nota_storico = ttk.Label(padre, foreground="#888", justify="left", wraplength=760)
        self.label_nota_storico.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        frame_controlli_storico = ttk.Frame(padre)
        frame_controlli_storico.grid(row=1, column=0, sticky="w", padx=8, pady=4)

        self.label_storico_giorno = ttk.Label(frame_controlli_storico)
        self.label_storico_giorno.pack(side="left")
        self.var_storico_giorno = tk.StringVar()
        self.combo_storico_giorno = ttk.Combobox(
            frame_controlli_storico, textvariable=self.var_storico_giorno,
            state="readonly", width=22, values=(),
        )
        self.combo_storico_giorno.pack(side="left", padx=(4, 16))
        self.combo_storico_giorno.bind("<<ComboboxSelected>>", lambda evento: self._aggiorna_storico())

        self.var_storico_includi_test = tk.BooleanVar()
        self.checkbutton_storico_test = ttk.Checkbutton(
            frame_controlli_storico, variable=self.var_storico_includi_test, command=self._aggiorna_storico,
        )
        self.checkbutton_storico_test.pack(side="left", padx=(0, 16))

        self.bottone_storico_aggiorna = ttk.Button(frame_controlli_storico, command=self._aggiorna_storico)
        self.bottone_storico_aggiorna.pack(side="left")

        self.frame_popolarita_storico = ttk.LabelFrame(padre)
        self.frame_popolarita_storico.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        colonne_popolarita = ("titolo", "voti", "vinte")
        self.albero_popolarita_storico = ttk.Treeview(
            self.frame_popolarita_storico, columns=colonne_popolarita, show="headings",
            height=6, selectmode="none",
        )
        for colonna, larghezza in (("titolo", 340), ("voti", 120), ("vinte", 180)):
            self.albero_popolarita_storico.column(
                colonna, width=larghezza, anchor="w" if colonna == "titolo" else "center",
            )
        self.albero_popolarita_storico.pack(fill="x", padx=8, pady=8)

        self.frame_cronologia_storico = ttk.LabelFrame(padre)
        self.frame_cronologia_storico.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        colonne_cronologia = ("ora", "titolo", "voti")
        self.albero_cronologia_storico = ttk.Treeview(
            self.frame_cronologia_storico, columns=colonne_cronologia, show="headings", selectmode="none",
        )
        for colonna, larghezza in (("ora", 90), ("titolo", 340), ("voti", 120)):
            self.albero_cronologia_storico.column(
                colonna, width=larghezza, anchor="w" if colonna == "titolo" else "center",
            )
        scrollbar_cronologia = ttk.Scrollbar(
            self.frame_cronologia_storico, orient="vertical", command=self.albero_cronologia_storico.yview,
        )
        self.albero_cronologia_storico.configure(yscrollcommand=scrollbar_cronologia.set)
        scrollbar_cronologia.pack(side="right", fill="y", pady=8)
        self.albero_cronologia_storico.pack(fill="both", expand=True, padx=(8, 0), pady=8)

        frame_bottoni_storico = ttk.Frame(padre)
        frame_bottoni_storico.grid(row=4, column=0, sticky="w", padx=8, pady=(4, 8))
        self.bottone_storico_esporta = ttk.Button(frame_bottoni_storico, command=self._esporta_storico_csv)
        self.bottone_storico_esporta.pack(side="left", padx=(0, 4))
        self.bottone_storico_elimina_giorno = ttk.Button(frame_bottoni_storico, command=self._elimina_giorno_storico)
        self.bottone_storico_elimina_giorno.pack(side="left")

    def _ritraduci_tab_storico(self):
        self.label_storico_giorno.configure(text=self._t("ms_storico_label_giorno"))
        self.checkbutton_storico_test.configure(text=self._t("ms_storico_checkbox_test"))
        self.bottone_storico_aggiorna.configure(text=self._t("ms_storico_bottone_aggiorna"))
        self.frame_popolarita_storico.configure(text=self._t("ms_storico_titolo_popolarita"))
        self.albero_popolarita_storico.heading("titolo", text=self._t("ms_storico_colonna_titolo"))
        self.albero_popolarita_storico.heading("voti", text=self._t("ms_storico_colonna_voti_totali"))
        self.albero_popolarita_storico.heading("vinte", text=self._t("ms_storico_colonna_volte_vinta"))
        self.albero_cronologia_storico.heading("ora", text=self._t("ms_storico_colonna_ora"))
        self.albero_cronologia_storico.heading("titolo", text=self._t("ms_storico_colonna_titolo"))
        self.albero_cronologia_storico.heading("voti", text=self._t("ms_storico_colonna_voti_turno"))
        self.bottone_storico_esporta.configure(text=self._t("ms_storico_bottone_esporta"))
        self.bottone_storico_elimina_giorno.configure(text=self._t("ms_storico_bottone_elimina_giorno"))
        self._aggiorna_storico()

    def _pianifica_aggiornamento_storico(self):
        """Aggiorna da sola le schede Storico/Grafico ogni pochi secondi,
        ma SOLO quella tra le due che e' visibile in questo momento: cosi'
        non si interroga il database quando non serve (es. mentre si
        lavora su un'altra scheda), e i dati si vedono muovere man mano
        che le sequenze passano, senza dover premere "Aggiorna" a mano."""
        try:
            scheda_attiva = self.notebook.index(self.notebook.select())
            if scheda_attiva == self.notebook.index(self.tab_storico):
                self._aggiorna_storico()
            elif scheda_attiva == self.notebook.index(self.tab_grafico):
                self._aggiorna_grafico(mostra_errori=False)
        except tk.TclError:
            pass  # finestra in chiusura: niente da aggiornare
        self.after(5000, self._pianifica_aggiornamento_storico)

    def _etichetta_giorno_storico(self, data_iso: str) -> str:
        """'2026-12-20' -> 'Lun 20/12/2026' (nome del giorno nella lingua
        attuale, riusando le stesse traduzioni della scheda Programmazione)."""
        try:
            oggetto_data = datetime.strptime(data_iso, "%Y-%m-%d")
        except ValueError:
            return data_iso
        nome_giorno = self._t(f"ms_giorno_{oggetto_data.weekday()}")
        return f"{nome_giorno} {oggetto_data.strftime('%d/%m/%Y')}"

    def _aggiorna_storico(self):
        for iid in self.albero_popolarita_storico.get_children():
            self.albero_popolarita_storico.delete(iid)
        for iid in self.albero_cronologia_storico.get_children():
            self.albero_cronologia_storico.delete(iid)

        if not FILE_DATABASE_STORICO.is_file():
            self.label_nota_storico.configure(text=self._t("ms_storico_db_non_trovato"))
            self.bottone_storico_elimina_giorno.configure(state="disabled")
            self.bottone_storico_esporta.configure(state="disabled")
            return

        self.label_nota_storico.configure(text=self._t("ms_storico_nota"))
        self.bottone_storico_esporta.configure(state="normal")

        etichetta_tutto = self._t("ms_storico_tutto_storico")
        # Mappa etichetta mostrata (col nome del giorno) -> data ISO vera,
        # usata per le query; None = "tutto lo storico".
        self._mappa_giorni_storico = {etichetta_tutto: None}
        for data_iso in date_disponibili_storico():
            self._mappa_giorni_storico[self._etichetta_giorno_storico(data_iso)] = data_iso

        valori_combo = list(self._mappa_giorni_storico.keys())
        selezione_precedente = self.var_storico_giorno.get()
        self.combo_storico_giorno.configure(values=valori_combo)
        if selezione_precedente not in valori_combo:
            self.var_storico_giorno.set(valori_combo[0])
        giorno_selezionato = self.var_storico_giorno.get()
        data_filtro = self._mappa_giorni_storico.get(giorno_selezionato)
        tutto = data_filtro is None

        includi_test = self.var_storico_includi_test.get()

        for titolo, voti, vinte in riepilogo_popolarita_storico(data_filtro, includi_test):
            self.albero_popolarita_storico.insert("", "end", values=(titolo, voti, vinte))

        if tutto:
            self.albero_cronologia_storico.insert(
                "", "end", values=("", self._t("ms_storico_nota_cronologia_tutto"), ""),
            )
            self.bottone_storico_elimina_giorno.configure(state="disabled")
        else:
            for ora, titolo, voti in cronologia_giorno_storico(data_filtro, includi_test):
                self.albero_cronologia_storico.insert("", "end", values=(ora, titolo, voti))
            self.bottone_storico_elimina_giorno.configure(state="normal")

    def _installa_matplotlib(self):
        vuole_installare = messagebox.askyesno(
            self._t("ms_storico_bottone_installa_grafico"), self._t("ms_storico_grafico_mancante_testo"),
        )
        if not vuole_installare:
            return
        risultato = subprocess.run(
            [sys.executable, "-m", "pip", "install", "matplotlib"], capture_output=True, text=True,
        )
        if risultato.returncode == 0:
            messagebox.showinfo(self._t("gc_installazione_riuscita_titolo"), self._t("ms_storico_grafico_installato_testo"))
        else:
            messagebox.showerror(
                self._t("gc_installazione_fallita_titolo"),
                self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
            )

    def _esporta_storico_csv(self):
        giorno_selezionato = self.var_storico_giorno.get()
        data_filtro = self._mappa_giorni_storico.get(giorno_selezionato)
        tutto = data_filtro is None
        includi_test = self.var_storico_includi_test.get()

        percorso = filedialog.asksaveasfilename(
            title=self._t("ms_storico_bottone_esporta"),
            defaultextension=".csv",
            initialfile=f"storico_voti_{'tutto' if tutto else data_filtro}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not percorso:
            return

        righe_popolarita = riepilogo_popolarita_storico(data_filtro, includi_test)
        righe_cronologia = [] if tutto else cronologia_giorno_storico(data_filtro, includi_test)

        try:
            # utf-8-sig + ';' cosi' Excel in italiano apre il file con gli
            # accenti giusti senza dover importare manualmente il CSV.
            with open(percorso, "w", newline="", encoding="utf-8-sig") as f:
                scrittore = csv.writer(f, delimiter=";")
                scrittore.writerow([self._t("ms_storico_titolo_popolarita")])
                scrittore.writerow([
                    self._t("ms_storico_colonna_titolo"),
                    self._t("ms_storico_colonna_voti_totali"),
                    self._t("ms_storico_colonna_volte_vinta"),
                ])
                scrittore.writerows(righe_popolarita)
                if not tutto:
                    scrittore.writerow([])
                    scrittore.writerow([self._t("ms_storico_titolo_cronologia")])
                    scrittore.writerow([
                        self._t("ms_storico_colonna_ora"),
                        self._t("ms_storico_colonna_titolo"),
                        self._t("ms_storico_colonna_voti_turno"),
                    ])
                    scrittore.writerows(righe_cronologia)
        except OSError as errore:
            messagebox.showerror(self._t("gc_errore_titolo"), self._t("gc_errore_salvataggio_file", errore=errore))
            return

        messagebox.showinfo(self._t("ms_storico_esportato_titolo"), self._t("ms_storico_esportato_testo", percorso=percorso))

    def _elimina_giorno_storico(self):
        giorno_selezionato = self.var_storico_giorno.get()
        data_filtro = self._mappa_giorni_storico.get(giorno_selezionato)
        if not data_filtro:
            return
        if not messagebox.askyesno(
            self._t("ms_storico_conferma_elimina_titolo"),
            self._t("ms_storico_conferma_elimina_testo", data=giorno_selezionato),
        ):
            return
        elimina_dati_giorno_storico(data_filtro)
        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_storico_eliminato_testo"))
        self._aggiorna_storico()

    # ------------------------------------------------------------------
    # Scheda Grafico: andamento dei voti totali giorno per giorno, nel
    # periodo dello show scelto (dal primo all'ultimo giorno, o un
    # intervallo piu' stretto se lo si restringe qui).
    # ------------------------------------------------------------------
    def _costruisci_tab_grafico(self, padre):
        self._mappa_giorni_grafico = {}  # etichetta (col nome del giorno) -> data ISO
        padre.columnconfigure(0, weight=1)

        self.label_nota_grafico = ttk.Label(padre, foreground="#888", justify="left", wraplength=760)
        self.label_nota_grafico.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        frame_controlli_grafico = ttk.Frame(padre)
        frame_controlli_grafico.grid(row=1, column=0, sticky="w", padx=8, pady=4)

        self.label_grafico_da = ttk.Label(frame_controlli_grafico)
        self.label_grafico_da.pack(side="left")
        self.var_grafico_da = tk.StringVar()
        self.combo_grafico_da = ttk.Combobox(
            frame_controlli_grafico, textvariable=self.var_grafico_da, state="normal", width=18, values=(),
        )
        self.combo_grafico_da.pack(side="left", padx=(4, 2))
        self.combo_grafico_da.bind("<<ComboboxSelected>>", lambda evento: self._aggiorna_grafico())
        self.combo_grafico_da.bind("<Return>", lambda evento: self._aggiorna_grafico())
        self.combo_grafico_da.bind("<FocusOut>", lambda evento: self._aggiorna_grafico())
        self.bottone_calendario_da = ttk.Button(
            frame_controlli_grafico, text="\U0001F4C5", width=3,
            command=lambda: self._apri_calendario_grafico(self.var_grafico_da),
        )
        self.bottone_calendario_da.pack(side="left", padx=(0, 16))

        self.label_grafico_a = ttk.Label(frame_controlli_grafico)
        self.label_grafico_a.pack(side="left")
        self.var_grafico_a = tk.StringVar()
        self.combo_grafico_a = ttk.Combobox(
            frame_controlli_grafico, textvariable=self.var_grafico_a, state="normal", width=18, values=(),
        )
        self.combo_grafico_a.pack(side="left", padx=(4, 2))
        self.combo_grafico_a.bind("<<ComboboxSelected>>", lambda evento: self._aggiorna_grafico())
        self.combo_grafico_a.bind("<Return>", lambda evento: self._aggiorna_grafico())
        self.combo_grafico_a.bind("<FocusOut>", lambda evento: self._aggiorna_grafico())
        self.bottone_calendario_a = ttk.Button(
            frame_controlli_grafico, text="\U0001F4C5", width=3,
            command=lambda: self._apri_calendario_grafico(self.var_grafico_a),
        )
        self.bottone_calendario_a.pack(side="left", padx=(0, 4))

        self.bottone_salva_range_grafico = ttk.Button(frame_controlli_grafico, command=self._salva_range_grafico)
        self.bottone_salva_range_grafico.pack(side="left", padx=(0, 16))

        self.var_grafico_includi_test = tk.BooleanVar()
        self.checkbutton_grafico_test = ttk.Checkbutton(
            frame_controlli_grafico, variable=self.var_grafico_includi_test, command=self._aggiorna_grafico,
        )
        self.checkbutton_grafico_test.pack(side="left", padx=(0, 16))

        self.bottone_grafico_aggiorna = ttk.Button(frame_controlli_grafico, command=self._aggiorna_grafico)
        self.bottone_grafico_aggiorna.pack(side="left")

        self.frame_grafico = ttk.LabelFrame(padre)
        self.frame_grafico.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        if MATPLOTLIB_DISPONIBILE:
            self.figura_grafico = Figure(figsize=(7.6, 3.4), dpi=90)
            self.assi_grafico = self.figura_grafico.add_subplot(111)
            self.canvas_grafico = FigureCanvasTkAgg(self.figura_grafico, master=self.frame_grafico)
            self.canvas_grafico.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)
        else:
            self.label_grafico_mancante = ttk.Label(
                self.frame_grafico, foreground="#888", justify="left", wraplength=560,
            )
            self.label_grafico_mancante.pack(side="left", padx=8, pady=8)
            self.bottone_installa_grafico = ttk.Button(self.frame_grafico, command=self._installa_matplotlib)
            self.bottone_installa_grafico.pack(side="left", padx=8, pady=8)

        self._carica_range_grafico_da_ini()

    def _carica_range_grafico_da_ini(self):
        """Se e' gia' stato salvato un periodo in precedenza (pulsante
        'Salva'), la scheda riparte da li' invece che dal periodo intero
        disponibile - vale finche' non lo si risalva su un periodo
        diverso."""
        parser = carica_parser()
        data_da = parser.get(SEZIONE_VOTO, "Grafico_Data_Da", fallback="").strip()
        data_a = parser.get(SEZIONE_VOTO, "Grafico_Data_A", fallback="").strip()
        if data_da:
            self.var_grafico_da.set(self._etichetta_giorno_storico(data_da))
        if data_a:
            self.var_grafico_a.set(self._etichetta_giorno_storico(data_a))

    def _salva_range_grafico(self):
        data_da = self._parse_data_grafico(self.var_grafico_da.get())
        data_a = self._parse_data_grafico(self.var_grafico_a.get())
        if data_da is None or data_a is None:
            messagebox.showerror(self._t("ms_grafico_data_non_valida_titolo"), self._t("ms_grafico_data_non_valida_testo"))
            return
        if data_da > data_a:
            data_da, data_a = data_a, data_da

        parser = carica_parser()
        parser[SEZIONE_VOTO]["Grafico_Data_Da"] = data_da
        parser[SEZIONE_VOTO]["Grafico_Data_A"] = data_a
        salva_parser(parser)

        messagebox.showinfo(self._t("gc_fatto_titolo"), self._t("ms_grafico_range_salvato_testo"))

    def _ritraduci_tab_grafico(self):
        self.label_nota_grafico.configure(text=self._t("ms_grafico_nota"))
        self.label_grafico_da.configure(text=self._t("ms_grafico_label_da"))
        self.label_grafico_a.configure(text=self._t("ms_grafico_label_a"))
        self.checkbutton_grafico_test.configure(text=self._t("ms_storico_checkbox_test"))
        self.bottone_grafico_aggiorna.configure(text=self._t("ms_storico_bottone_aggiorna"))
        self.bottone_salva_range_grafico.configure(text=self._t("gc_bottone_salva"))
        self.frame_grafico.configure(text=self._t("ms_grafico_titolo"))
        if not MATPLOTLIB_DISPONIBILE:
            self.label_grafico_mancante.configure(text=self._t("ms_storico_grafico_mancante_testo"))
            self.bottone_installa_grafico.configure(text=self._t("ms_storico_bottone_installa_grafico"))
        self._aggiorna_grafico()

    def _parse_data_grafico(self, testo: str):
        """Converte il testo di una combobox data (etichetta con nome
        giorno dalla tendina/calendario, oppure scritta a mano in vari
        formati) nella data ISO corrispondente. None se non riconosciuto."""
        testo = testo.strip()
        if not testo:
            return None
        if testo in self._mappa_giorni_grafico:
            return self._mappa_giorni_grafico[testo]
        for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(testo, formato).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Etichetta con nome del giorno ("Ven 18/12/2026") ma la data non
        # e' (piu') nella mappa - es. un range salvato per un giorno che
        # nel frattempo non ha piu' voti registrati: si riprova ignorando
        # il nome del giorno, tenendo solo l'ultima parola (la data vera).
        ultima_parola = testo.rsplit(" ", 1)[-1]
        if ultima_parola != testo:
            try:
                return datetime.strptime(ultima_parola, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    def _aggiorna_grafico(self, mostra_errori: bool = True):
        """mostra_errori=False durante l'aggiornamento automatico
        periodico: se in quel momento l'utente sta scrivendo una data a
        mano e il testo e' temporaneamente incompleto/non valido, non ha
        senso interromperlo con un popup di errore ogni 5 secondi - si
        salta silenziosamente e si riprova al giro successivo. Con
        mostra_errori=True (tendina, calendario, invio, uscita dal
        campo) l'errore viene invece segnalato subito."""
        if not FILE_DATABASE_STORICO.is_file():
            self.label_nota_grafico.configure(text=self._t("ms_storico_db_non_trovato"))
            if MATPLOTLIB_DISPONIBILE:
                self._disegna_grafico([])
            return
        self.label_nota_grafico.configure(text=self._t("ms_grafico_nota"))

        date_disponibili = date_disponibili_storico()  # dalla piu' recente alla piu' vecchia
        self._mappa_giorni_grafico = {
            self._etichetta_giorno_storico(data_iso): data_iso for data_iso in reversed(date_disponibili)
        }
        valori_combo = list(self._mappa_giorni_grafico.keys())
        self.combo_grafico_da.configure(values=valori_combo)
        self.combo_grafico_a.configure(values=valori_combo)

        if not valori_combo:
            if MATPLOTLIB_DISPONIBILE:
                self._disegna_grafico([])
            return

        # Il default (tutto il periodo disponibile) si propone solo se il
        # campo e' ancora vuoto: un valore gia' scritto/scelto non viene
        # mai sovrascritto qui, altrimenti si perderebbe quello che
        # l'utente sta digitando ad ogni aggiornamento automatico.
        if not self.var_grafico_da.get().strip():
            self.var_grafico_da.set(valori_combo[0])
        if not self.var_grafico_a.get().strip():
            self.var_grafico_a.set(valori_combo[-1])

        data_da = self._parse_data_grafico(self.var_grafico_da.get())
        data_a = self._parse_data_grafico(self.var_grafico_a.get())
        if data_da is None or data_a is None:
            if mostra_errori:
                messagebox.showerror(
                    self._t("ms_grafico_data_non_valida_titolo"), self._t("ms_grafico_data_non_valida_testo"),
                )
            return
        if data_da > data_a:
            data_da, data_a = data_a, data_da  # l'utente ha invertito Da/A: si scambia in silenzio

        if MATPLOTLIB_DISPONIBILE:
            includi_test = self.var_grafico_includi_test.get()
            dati = andamento_voti_giornalieri_storico(includi_test, data_da, data_a)
            self._disegna_grafico(dati, data_da, data_a)

    def _apri_calendario_grafico(self, var_destinazione: tk.StringVar):
        if not TKCALENDAR_DISPONIBILE:
            vuole_installare = messagebox.askyesno(
                self._t("ms_grafico_calendario_titolo"), self._t("ms_grafico_calendario_mancante_testo"),
            )
            if vuole_installare:
                risultato = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "tkcalendar"], capture_output=True, text=True,
                )
                if risultato.returncode == 0:
                    messagebox.showinfo(
                        self._t("gc_installazione_riuscita_titolo"), self._t("ms_grafico_calendario_installato_testo"),
                    )
                else:
                    messagebox.showerror(
                        self._t("gc_installazione_fallita_titolo"),
                        self._t("gc_installazione_fallita_testo", errore=risultato.stderr[-500:]),
                    )
            return

        finestra = tk.Toplevel(self)
        finestra.title(self._t("ms_grafico_calendario_titolo"))
        finestra.resizable(False, False)
        finestra.transient(self)
        finestra.grab_set()

        kwargs_data = {}
        data_iniziale = self._parse_data_grafico(var_destinazione.get())
        if data_iniziale:
            anno, mese, giorno = (int(pezzo) for pezzo in data_iniziale.split("-"))
            kwargs_data = {"year": anno, "month": mese, "day": giorno}

        calendario = WidgetCalendario(finestra, date_pattern="dd/mm/yyyy", **kwargs_data)
        calendario.pack(padx=8, pady=8)

        def _scegli():
            var_destinazione.set(calendario.get_date())
            finestra.destroy()
            self._aggiorna_grafico()

        ttk.Button(finestra, text=self._t("gc_bottone_salva"), command=_scegli).pack(pady=(0, 8))

        finestra.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - finestra.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - finestra.winfo_height()) // 2
        finestra.geometry(f"+{x}+{y}")

    def _disegna_grafico(self, dati, data_da: str = None, data_a: str = None):
        self.assi_grafico.clear()
        giorni_iso = []
        if data_da and data_a:
            # Solo giovedi'/sabato/domenica nel periodo (di solito i
            # giorni piu' votati): tutti gli altri giorni della settimana
            # non compaiono affatto sull'asse. Un punto anche per quelli
            # senza voti (a 0), non solo per quelli che ne hanno ricevuti.
            giorni_da_mostrare = {3, 5, 6}  # weekday(): 0=lunedi' ... 6=domenica
            cursore = datetime.strptime(data_da, "%Y-%m-%d")
            fine = datetime.strptime(data_a, "%Y-%m-%d")
            while cursore <= fine:
                if cursore.weekday() in giorni_da_mostrare:
                    giorni_iso.append(cursore.strftime("%Y-%m-%d"))
                cursore += timedelta(days=1)

        if giorni_iso:
            conteggio_per_giorno = dict(dati)
            etichette = [self._etichetta_giorno_storico(giorno) for giorno in giorni_iso]
            valori = [conteggio_per_giorno.get(giorno, 0) for giorno in giorni_iso]
            self.assi_grafico.plot(etichette, valori, marker="o", color="#2e7d32")
            # Il primo giorno attaccato all'asse dei voti: xlim impostato
            # a mano sulle posizioni intere dei punti (0, 1, 2, ...) invece
            # di affidarsi al margine automatico di matplotlib (che lascia
            # sempre un vuoto anche a zero, a seconda della versione/backend).
            # Un pizzico di margine resta solo a destra, per non tagliare
            # l'ultimo pallino/etichetta.
            ultima_posizione = len(etichette) - 1
            self.assi_grafico.set_xlim(-0.05, ultima_posizione + 0.4)
            self.figura_grafico.autofmt_xdate(rotation=30, ha="right")
        else:
            self.assi_grafico.text(
                0.5, 0.5, self._t("ms_grafico_nessun_dato"),
                ha="center", va="center", transform=self.assi_grafico.transAxes, color="#888",
            )
        self.assi_grafico.set_ylabel(self._t("ms_grafico_asse_voti"))
        self.figura_grafico.tight_layout()
        self.canvas_grafico.draw()


if __name__ == "__main__":
    # L'elevazione (se serve) e' richiesta dal collegamento stesso ("Esegui
    # sempre come amministratore" nelle proprieta' avanzate del file .lnk),
    # non piu' da un rilancio automatico qui dentro: quel trucco creava un
    # secondo processo (con la finestra UAC in mezzo) che la barra delle
    # applicazioni vedeva come un programma diverso da quello agganciato
    # all'icona cliccata, con una seconda icona separata. Con l'elevazione
    # gestita dal collegamento, Windows la richiede una volta sola PRIMA di
    # creare il processo: resta un solo processo, un'icona sola - se
    # l'utente rifiuta il prompt UAC, o lancia il file senza passare dal
    # collegamento elevato, ManagerShow parte comunque, solo senza privilegi
    # elevati (vedi _processo_e_elevato, usato piu' sotto per gli avvisi).
    try:
        # Va chiamato PRIMA di creare qualunque finestra: dice a Windows che
        # questo processo e' un'app a se stante, distinta da SchedeLuci.pyw
        # (lanciato da qui ma con un suo processo pythonw.exe separato) -
        # senza, la barra delle applicazioni puo' raggruppare/confondere le
        # icone dei due programmi perche' condividono lo stesso eseguibile
        # (pythonw.exe).
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VotoShow.ManagerShow")
    except Exception:
        pass
    app = ManagerShow()
    app.mainloop()
