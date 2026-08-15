# Handoff — progetto VotoShow (voto pubblico show luci natalizie)

## Percorso progetto
`E:\04_SHOW\2026\01_FILE_OPERATIVI\01_CONTROLLO_VOTO\02_SERVER_VOTO\`
File principali: `VotoShow.py` (server HTTP live, stdlib+sqlite3 solo), `ManagerShow.pyw` (GUI Tkinter di gestione), `traduzioni.py` (dizionario IT/EN condiviso), `CostruisciCatalogo.py`.
Config: `..\01_AUTOMAZIONE\CONFIGURAZIONE_SHOW.ini`.

Non è un repo git. Verificare sempre con `python -m py_compile` dopo ogni modifica (niente linter/test automatico nel progetto).

## Cosa è stato implementato in questa sessione (in ordine)
1. **Tie-break per timestamp** nei pareggi di voto (`calcola_classifica`), invece di lasciare il pareggio irrisolto.
2. **Pagina di benvenuto** su `/` (prima della pagina di voto, spostata su `/vota`), configurabile da GUI (testo a paragrafi, immagine JPG/PNG/GIF, audio, colore auto-estratto + color picker), layout mai sovrapposto, colore testo automatico bianco/nero per contrasto.
3. **Coda persistente configurabile**: 3 modalità (`turno` = vecchio comportamento nessuna coda, `persistente` = coda senza limiti, `tetto` = coda con limite configurabile), selezionabili da GUI, con pulsante "Salva" che offre riavvio automatico di VotoShow.py.
4. **Test multi-voto**: pulsante GUI che apre N finestre browser con profili Chrome/Edge isolati puntate su `127.0.0.1` (loopback esente da rate-limit, mai raggiungibile da un ospite vero).
5. **Database storico SQLite** (`LOG/storico_voti.sqlite3`, tabelle `voti` e `vincitori`, marcatura automatica `test` per voti da loopback). Scheda GUI "Storico" (popolarità + cronologia giorno, esporta CSV, elimina giorno) e scheda separata "Grafico" (andamento voti/giorno, solo giovedì/sabato/domenica, range Da/A con calendario opzionale via `tkcalendar`, range salvabile).
6. **Raffreddamento configurabile** (turni o minuti, non più fisso a 3 turni), con comportamento voto differenziato per modalità coda: in "turno" un voto su sequenza in pausa viene **rifiutato** (notifica), nelle altre modalità viene **accettato** (notifica "verrà messa in coda"). Etichetta "(in pausa)" nella pagina risultati.
7. **Pagina risultati coerente con la modalità coda attiva**: mostra "Modalità coda: ..." in alto, nota della colonna "in coda" adattata, colonna "in coda" nascosta in modalità "persistente" (ridondante con la colonna centrale).
8. **Header anti-cache** (`Cache-Control: no-store`, `Pragma: no-cache`) su tutte le pagine HTML, per evitare che il browser mostri contenuti vecchi.

Tutto testato con simulazioni dirette in Python (mock delle chiamate API LOR, costruzione reale di `ManagerShow` in test headless, ecc.) — vedi lo storico dei comandi bash in questa conversazione se serve rifare un test.

## PROBLEMA APERTO (in corso di diagnosi, ultimo argomento)
L'utente segnala che la pagina `/risultati` non mostra la modalità coda appena salvata/riavviata (mostra sempre "coda senza limiti" / persistente), **anche dopo stop/avvio manuale e riapertura pagina** (quindi non è cache browser, già escluso e corretto col fix del punto 8).

Diagnosi fatta finora:
- Il file di log (`LOG/VotoShow_AAAAMMGG.log`) conferma che **ogni riavvio logga correttamente** la nuova modalità scelta (verificato: persistente → tetto → turno → tetto, tutte corrette nel log).
- L'utente ha trovato **2 processi Python** in Gestione attività entrambi relativi a "server voto" — sospetto forte: un processo VotoShow.py rimasto orfano/attaccato alla porta 8080 da una sessione di test precedente (durante questa lunga conversazione ci sono stati moltissimi riavvii), che continua a rispondere con la config vecchia mentre un secondo processo "nuovo" logga correttamente ma potrebbe non essere quello che risponde realmente sulla porta.
- L'utente ha chiuso tutti i processi Python (ora zero in esecuzione).

**Prossimo passo da chiedere/verificare all'utente**: riaprire `ManagerShow.pyw` pulito, premere "Avvia ora" UNA sola volta, controllare `/risultati` — se ora mostra la modalità corretta, il problema era il processo orfano (spiegare come evitarlo: non sovrapporre più riavvii ravvicinati, ev. aggiungere in futuro un controllo più robusto in `FermaShow.ferma()`/`AvviaShow.avvia()` per verificare che la porta sia davvero libera, non solo il PID file). Se mostra ancora quella sbagliata anche con un solo processo pulito, allora c'è un bug vero nella lettura di `modalita_coda_attiva` in `VotoShow.py` da indagare nel codice (non ancora escluso al 100%, ma indiziato come processo-orfano).

## Note di stile/collaborazione utente
- L'utente scrive spesso in maiuscolo (non è enfasi rabbiosa, solo abitudine).
- Preferisce risposte dirette, test concreti prima di dichiarare "fatto", niente promesse non verificate.
- Legge poco l'inglese tecnico: tenere le spiegazioni in italiano semplice.
- Tende a interrompere/correggere messaggi a metà — leggere sempre l'ultimo messaggio per intero prima di agire.
