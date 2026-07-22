# Audit bug, performance e feature inattive — 22 luglio 2026

## Esito

L'audit ha portato lo smoke test da **5 fallimenti a 0** e ha corretto leak di processi Qt,
riferimenti LSP, timer MAPPA e crescita illimitata del terminale/log AI.

Verifica finale:

- `python -m compileall -q kant kant_editor.py test_kant_smoke.py`: OK
- `python test_kant_smoke.py`: **125 test OK**, 1 skip per un toolchain opzionale non installato
- parser Android `ParserSelfTest`: **OK**; build APK fermata dall'SDK locale con licenze non accettate
- `git diff --check`: nessun errore di whitespace

## Problemi corretti

| Gravità | Problema | Correzione |
| --- | --- | --- |
| Alta | Codex su Windows non riusciva ad avviare alcun comando (`CreateProcessWithLogonW failed: 2`). | Il launcher forza il fallback ufficiale `windows.sandbox="unelevated"`, mantenendo `workspace-write`; nessun passaggio a `danger-full-access`. |
| Alta | Ogni comando del terminale e ogni turno Claude/Codex lasciavano il vecchio `QProcess` come figlio Qt fino alla chiusura dell'IDE. | I processi conclusi o falliti vengono ora rimossi con `deleteLater()` in un unico punto del rispettivo pannello. |
| Alta | I breakpoint PDB venivano scritti su stdin immediatamente dopo `start()`, prima che il processo fosse realmente avviato; Qt poteva scartarli. | I comandi `break` e `continue` partono dal segnale `QProcess.started`. |
| Media | Completion e hover LSP senza risposta conservavano riferimenti ai `CodeEdit`, impedendo la liberazione degli editor chiusi. | Una sola richiesta pendente per editor; cleanup alla chiusura del tab e su errore del server. |
| Media | I retry LSP potevano scattare dopo un cambio tab ed eseguire l'azione sul file sbagliato. | Ogni retry conserva e verifica il percorso originale. |
| Media | Chiudere MAPPA durante un caricamento lasciava attivi spinner/animazioni; un errore xref lasciava lo spinner indefinitamente. | `hideEvent` ferma tutti i timer transitori; l'errore di build chiude lo stato loading e mostra un messaggio. |
| Media | Terminale e `kant-ai-terminal.log` crescevano senza limite. | Terminale limitato agli ultimi 10.000 blocchi; log AI ruotato a 5 MB. |
| Bassa | Le permission card risolte restavano nell'elenco operativo e la card di review staccata restava allocata. | Rimozione immediata dai registri e `deleteLater()` della review risolta. |
| Bassa | Il build xref rileggeva e riparsava dal disco anche i file aperti, per poi sovrascriverli con la versione in memoria. | I file aperti saltano la prima lettura: un solo parse per build. |
| Bassa | La maniglia MAPPA tornava nello `shell` mantenendo coordinate relative alla finestra precedente. | Riposizionamento subito dopo il reparent. |
| Bassa | Il pulsante metadati aveva solo l'icona, senza testo fallback/accessibile. | Ripristinata l'etichetta `⋮`, lasciando la visualizzazione icon-only. |
| Bassa | Il padding reale dell'albero era divergente dalla specifica e dal test di regressione. | Ripristinato il valore compatto `6px 4px`. |
| Ambiente | `pytest` era dichiarato in `requirements.txt` ma assente dalla `.venv`, causando due falsi fallimenti come semplice `exit code 1`. | Riallineata la `.venv` installando il requisito già dichiarato. |

## Feature presenti ma non attive o incomplete

### 1. Reveal automatico di una sezione KANT

`MainWindow._reveal_section()` è completo: espande tutti gli antenati e porta la sezione nello
scroll visibile. Non ha però alcun chiamante. La navigazione attuale apre/isola la sezione in un
tab e non usa mai questo percorso. È codice di una UX alternativa rimasta scollegata.

**Decisione suggerita:** collegarlo solo se si vuole una navigazione “rivela nel file” distinta
dall'attuale “apri elemento”; altrimenti eliminarlo.

### 2. Messaggio di installazione server LSP

`MainWindow._lsp_missing_server_message()` costruisce un messaggio preciso con i server disponibili,
ma in produzione non viene chiamato. Quando manca il server, `_lsp_command()` passa direttamente al
fallback locale. Il metodo è raggiunto soltanto da un test.

**Decisione suggerita:** mostrarlo nello status LSP senza bloccare il fallback, oppure rimuovere
metodo e test se il fallback silenzioso è il comportamento desiderato.

### 3. Gruppi KANT non rappresentati in MAPPA

I gruppi sono persistiti, riconciliati dopo rename/edit e visibili nella tree view, ma `mappa.py`
non importa né disegna i groupings. Il changelog stesso li descrive come follow-up non realizzato.

**Parte mancante:** overlay/confini/evidenziazione dei gruppi nella mappa, con aggiornamento quando i
membri vengono riconciliati.

### 4. PURE AI: conversazioni Codex visivamente separate, resume non separato

Le conversazioni PURE AI conservano transcript e stato per progetto. Claude salva un vero session ID;
Codex usa ancora `codex exec resume --last`. Se si alternano più conversazioni Codex nello stesso
progetto, il transcript mostrato è quello scelto ma il backend può riprendere l'ultima sessione CLI,
non quella specifica.

**Parte mancante:** acquisire e persistere il session ID emesso da Codex, poi usare
`exec resume <id>` quando la versione CLI installata lo supporta.

### 5. Companion App Android non ancora distribuita

Il precedente prototipo desktop è stato sostituito da un progetto Android nativo. Ha WebView GitHub
limitata al dominio, rilevamento e download autenticato FLOW/MAPPA, libreria privata e MAPPA touch
con heatmap, ricerca, focus, collapse, zoom e posizionamento manuale. Rimane separata dall'IDE ed
esclusa da Git come richiesto. Il parser passa il self-check; non è stato possibile produrre l'APK
in questa sessione perché l'SDK Android locale richiede l'accettazione delle licenze fuori workspace.

### 6. Maniglia MAPPA chiusa: codice rimasto da una UX precedente

`map_tab_btn` conserva styling, posizionamento e tooltip per lo stato chiuso sul bordo inferiore,
ma viene nascosto quando MAPPA è chiusa. Oggi serve soltanto come pulsante di chiusura sopra il dialog;
l'ingresso reale è il tasto MAPPA nella barra Incoming/Outgoing.

**Decisione suggerita:** eliminare il ramo/stile dello stato chiuso oppure rendere di nuovo visibile
la maniglia inferiore; mantenere entrambi è stato duplicato.

### 7. Helper morto per identità KANT

`MainWindow._kant_identity_text()` non ha chiamanti ed è stato superato da `_kant_identity_node()` e
dalla formattazione HTML specifica di tab/titlebar. Non è una feature, solo debito eliminabile.

## Rischi rimasti, non corretti automaticamente

### Scansioni complete dopo ogni salvataggio

MAPPA Markdown e FLOW CSV eseguono due scansioni complete indipendenti. Sono già in background,
coalescenti e write-if-changed, quindi su progetti normali il comportamento è corretto. Su repository
molto grandi il costo resta duplicato. Condividere un indice incrementale toccherebbe invarianti di
parser, watcher e generated files: farlo soltanto dopo un profiling su un progetto realmente lento.

### Task background non interrompibili durante la chiusura

`ThreadPoolExecutor.shutdown(cancel_futures=True)` annulla i job non iniziati, non quelli già in
esecuzione. Una scansione o un subprocess già partito può quindi terminare dopo la chiusura della
finestra. I callback UI sono già bloccati da `_closing`, ma l'uscita del processo può essere ritardata
fino al timeout del job. La soluzione corretta richiede cancellazione cooperativa o migrazione dei
subprocess lunghi a `QProcess`, non una kill generica inserita alla cieca.

### Storico PURE AI senza retention

Tutte le conversazioni sono salvate come un singolo JSON in `QSettings`, senza limite di numero o
dimensione. È semplice e corretto oggi, ma molte conversazioni lunghe renderanno ogni salvataggio più
costoso. Migrare a SQLite/file per conversazione soltanto quando il file reale supera una soglia
misurabile; aggiungere ora un secondo storage sarebbe prematuro.

### Warning `supports_reasoning_summaries`

Il warning proviene da `~/.codex/models_cache.json` scritto da una versione con schema diverso dalla
CLI 0.144.1. Non blocca l'esecuzione ed è stato deliberatamente lasciato a Codex: KANT non deve
modificare automaticamente cache interne OpenAI con semantica non documentata.

## Nota sullo scope

`legacy/index.html` è intenzionalmente fuori dal runtime corrente, come dichiarato in `PROJECT_MAP.md`;
non è stato contato come feature incompleta. Non sono state modificate né cancellate le altre modifiche
già presenti nel working tree.
