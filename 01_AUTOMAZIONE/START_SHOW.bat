@echo off
:: Messaggio di avviso per la gestione manuale o automatica
echo ==================================================
echo       CONFIGURAZIONE AVVIO SHOW NATALE 2026
echo ==================================================
echo.
echo Il PC si e' avviato. Vuoi avviare lo show?
echo.
echo Se non premi nulla, lo show partira' tra 10 secondi.
echo.

:: La riga 'choice' crea l'interruttore S/N
choice /C SN /M "Avviare lo show ora?" /T 10 /D S

:: Se l'utente preme N (errore 2), lo script si chiude
if errorlevel 2 goto :FINE

:: Se l'utente preme S (errore 1), lo show parte
:AVVIO
echo Avvio del Motore Luci in corso...
:: Esempio: start "" "E:\SHOW_NATALE_2026\02_SEQUENZE\LOR_Control_Panel.exe"
:: Aggiungeremo qui i comandi reali man mano che procediamo
exit

:FINE
echo Show annullato. Il PC resta libero per l'uso manuale.
exit