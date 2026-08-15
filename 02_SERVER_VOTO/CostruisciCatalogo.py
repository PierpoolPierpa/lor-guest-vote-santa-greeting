"""
CostruisciCatalogo.py
Strumento OFFLINE (da lanciare a mano, non durante lo show) che scansiona
la cartella delle sequenze e costruisce catalogo_canzoni.json: l'elenco
delle canzoni votabili con titolo, artista, durata e copertina.

Il server di voto (VotoShow.py), che gira durante lo show, legge solo
questo JSON gia' pronto: non tocca mai mp3/wav/wma ne' la libreria
mutagen mentre lo show e' in corso, per restare leggero e stabile.

Richiede il pacchetto 'mutagen' (pip install mutagen).
"""

import configparser
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CARTELLA_AUTOMAZIONE = SCRIPT_DIR.parent / "01_AUTOMAZIONE"
CONFIG_PATH = CARTELLA_AUTOMAZIONE / "CONFIGURAZIONE_SHOW.ini"
CARTELLA_COPERTINE = SCRIPT_DIR / "COPERTINE"
FILE_CATALOGO = SCRIPT_DIR / "catalogo_canzoni.json"

ESTENSIONI_AUDIO = (".mp3", ".wma", ".wav", ".m4a", ".flac")
ESTENSIONI_IMMAGINE = (".jpg", ".jpeg", ".png")


def _leggi_lingua_iniziale() -> str:
    """Letta prima ancora di importare mutagen, cosi' anche l'eventuale
    messaggio 'manca mutagen' viene mostrato nella lingua giusta."""
    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")
    lingua = parser.get("VOTO", "Lingua_Interfaccia", fallback="it").strip().lower()
    return lingua if lingua in ("it", "en") else "it"


LINGUA = _leggi_lingua_iniziale()

from traduzioni import testo  # noqa: E402 (import dopo LINGUA di proposito)

try:
    from mutagen import File as MutagenFile
except ImportError:
    print(testo(LINGUA, "cc_errore_mutagen"))
    try:
        risposta = input(testo(LINGUA, "cc_chiedi_installazione")).strip().lower()
    except (EOFError, OSError):
        risposta = ""
    if risposta in ("s", "si", "y", "yes"):
        # sys.executable garantisce che l'installazione vada nello stesso
        # identico Python che sta eseguendo questo script, senza ambiguita'
        # tra piu' installazioni diverse sullo stesso PC.
        risultato = subprocess.run([sys.executable, "-m", "pip", "install", "mutagen"])
        if risultato.returncode == 0:
            print(testo(LINGUA, "cc_installazione_riuscita"))
        else:
            print(testo(LINGUA, "cc_installazione_fallita"))
    sys.exit(1)


def leggi_configurazione():
    """Legge Cartella_Sequenze e Cartelle_Escluse dal CONFIGURAZIONE_SHOW.ini
    condiviso con MotoreShow.py, cosi' non c'e' un percorso duplicato."""
    if not CONFIG_PATH.is_file():
        print(testo(LINGUA, "cc_errore_config_mancante", percorso=CONFIG_PATH))
        sys.exit(1)

    parser = configparser.ConfigParser()
    parser.read(CONFIG_PATH, encoding="utf-8")

    cartella_sequenze_raw = parser.get("PERCORSI", "Cartella_Sequenze", fallback="").strip()
    if not cartella_sequenze_raw:
        print(testo(LINGUA, "cc_errore_cartella_sequenze_non_configurata"))
        sys.exit(1)

    cartella_sequenze = (CARTELLA_AUTOMAZIONE / cartella_sequenze_raw).resolve()
    if not cartella_sequenze.is_dir():
        print(testo(LINGUA, "cc_errore_cartella_sequenze_non_trovata", percorso=cartella_sequenze))
        sys.exit(1)

    escluse_raw = parser.get("VOTO", "Cartelle_Escluse", fallback="").strip()
    cartelle_escluse = {c.strip().lower() for c in escluse_raw.split(",") if c.strip()}

    return cartella_sequenze, cartelle_escluse


def crea_id(nome_cartella: str) -> str:
    """Trasforma il nome di una cartella in un id semplice e stabile,
    usato come chiave voto e come nome del file copertina copiato."""
    id_pulito = re.sub(r"[^a-z0-9]+", "_", nome_cartella.strip().lower())
    return id_pulito.strip("_")


def trova_primo_file(cartella: Path, estensioni: tuple) -> Path | None:
    for percorso in sorted(cartella.iterdir()):
        if percorso.is_file() and percorso.suffix.lower() in estensioni:
            return percorso
    return None


def formatta_durata(secondi: float) -> str:
    secondi_interi = int(round(secondi))
    minuti, secondi_rimanenti = divmod(secondi_interi, 60)
    return f"{minuti:02d}:{secondi_rimanenti:02d}"


def leggi_metadati_audio(percorso_audio: Path, nome_di_riserva: str):
    """Legge titolo/artista/durata dal file audio con mutagen. Se i tag
    mancano usa il nome della cartella come titolo, senza bloccarsi."""
    titolo = nome_di_riserva
    artista = ""
    durata_secondi = 0.0

    try:
        audio = MutagenFile(percorso_audio, easy=True)
    except Exception as errore:
        print(testo(LINGUA, "cc_errore_lettura_tag", nome=percorso_audio.name, errore=errore))
        audio = None

    if audio is not None:
        if audio.tags:
            # Chiavi diverse a seconda del formato: mp3/flac/... usano
            # "title"/"artist" (tag "easy"), i file .wma/.wav (ASF) usano
            # "Title"/"Author" con lettera maiuscola.
            titolo = _primo_valore(audio.tags, ("title", "Title")) or titolo
            artista = _primo_valore(audio.tags, ("artist", "Author")) or ""
        if audio.info is not None and getattr(audio.info, "length", 0):
            durata_secondi = audio.info.length

    return titolo, artista, durata_secondi


def _primo_valore(tags, chiavi_possibili) -> str | None:
    """Cerca la prima chiave presente tra quelle passate e ne ritorna il
    primo valore come stringa, gestendo sia i tag 'easy' (liste di
    stringhe) sia gli oggetti attributo di mutagen.asf (WMA)."""
    for chiave in chiavi_possibili:
        valori = tags.get(chiave)
        if valori:
            valore = str(valori[0]).strip()
            if valore:
                return valore
    return None


def scrivi_metadati_audio(percorso_audio: Path, titolo: str, artista: str) -> None:
    """Scrive titolo/artista nei tag del file audio, cosi' una correzione
    fatta dalla GUI resta permanente e non viene persa al prossimo
    ricalcolo del catalogo. Gestisce sia i tag 'easy' (mp3/flac/...) sia
    quelli ASF (wma), simmetricamente a leggi_metadati_audio()."""
    audio = MutagenFile(percorso_audio, easy=True)
    if audio is None:
        raise ValueError(f"Formato audio non riconosciuto: {percorso_audio.name}")

    if audio.tags is None:
        audio.add_tags()

    e_asf = percorso_audio.suffix.lower() in (".wma", ".wmv")
    if e_asf:
        audio.tags["Title"] = titolo
        audio.tags["Author"] = artista
    else:
        audio.tags["title"] = titolo
        audio.tags["artist"] = artista

    audio.save()


def elabora_sequenza(cartella: Path) -> dict | None:
    """Costruisce la voce di catalogo per una singola cartella-sequenza.
    Ritorna None se manca l'audio (nulla da votare)."""
    percorso_audio = trova_primo_file(cartella, ESTENSIONI_AUDIO)
    if percorso_audio is None:
        print(testo(LINGUA, "cc_nessun_audio", nome=cartella.name))
        return None

    titolo, artista, durata_secondi = leggi_metadati_audio(percorso_audio, cartella.name)

    percorso_copertina = trova_primo_file(cartella, ESTENSIONI_IMMAGINE)
    id_canzone = crea_id(cartella.name)
    nome_copertina_pubblicata = None

    if percorso_copertina is not None:
        nome_copertina_pubblicata = f"{id_canzone}{percorso_copertina.suffix.lower()}"
        CARTELLA_COPERTINE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(percorso_copertina, CARTELLA_COPERTINE / nome_copertina_pubblicata)
    else:
        print(testo(LINGUA, "cc_nessuna_copertina", nome=cartella.name))

    return {
        "id": id_canzone,
        "titolo": titolo,
        "artista": artista,
        "durata_secondi": round(durata_secondi),
        "durata_testo": formatta_durata(durata_secondi),
        "copertina": nome_copertina_pubblicata,
        "cartella_sorgente": cartella.name,
    }


def costruisci_catalogo():
    cartella_sequenze, cartelle_escluse = leggi_configurazione()
    print(testo(LINGUA, "cc_scansione_di", percorso=cartella_sequenze))

    catalogo = []
    for cartella in sorted(p for p in cartella_sequenze.iterdir() if p.is_dir()):
        if cartella.name.lower() in cartelle_escluse:
            print(testo(LINGUA, "cc_cartella_esclusa", nome=cartella.name))
            continue

        print(testo(LINGUA, "cc_cartella_nome", nome=cartella.name))
        voce = elabora_sequenza(cartella)
        if voce is not None:
            catalogo.append(voce)

    with open(FILE_CATALOGO, "w", encoding="utf-8") as f:
        json.dump(catalogo, f, ensure_ascii=False, indent=2)

    print(testo(LINGUA, "cc_catalogo_salvato", percorso=FILE_CATALOGO))
    print(testo(LINGUA, "cc_canzoni_votabili", numero=len(catalogo)))

    senza_copertina = [v["titolo"] for v in catalogo if v["copertina"] is None]
    if senza_copertina:
        print(testo(LINGUA, "cc_attenzione_senza_copertina"))
        for titolo in senza_copertina:
            print(f"  - {titolo}")


if __name__ == "__main__":
    costruisci_catalogo()
