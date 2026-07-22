"""Small reversible EN/IT translation layer for the Qt interface."""
from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QDialog, QGroupBox, QLabel, QLineEdit, QMenu, QPlainTextEdit,
    QTabWidget, QTextEdit, QWidget,
)


# The UI was originally written in Italian. Keeping those strings as the canonical keys lets the
# existing constructors stay simple while one post-pass translates both current and later dialogs.
IT_TO_EN = {
    # Common controls
    'Si': 'Yes',
    'No': 'No',
    'Annulla': 'Cancel',
    'Chiudi': 'Close',
    'Crea': 'Create',
    'Salva': 'Save',
    'Elimina': 'Delete',
    'Rinomina…': 'Rename…',
    'Aggiorna': 'Refresh',
    'Cambia': 'Switch',
    'Avvia': 'Start',
    'Invia': 'Send',
    'Stop': 'Stop',
    '(predefinito)': '(default)',
    'Terminale': 'Terminal',
    'Problemi': 'Problems',
    'MAPPA': 'MAP',
    'Sfoglia...': 'Browse…',
    'Conferma e continua': 'Confirm and continue',
    'Sostituisci selezionati': 'Replace selected',
    'Stage tutto': 'Stage all',
    'Crea progetto': 'Create project',
    'Vai alla riga': 'Go to line',
    'Applica fix': 'Apply fix',
    'Ripristina originali': 'Restore originals',
    'Tieni i file attuali': 'Keep current files',
    'Nessun risultato': 'No results',
    'Cestino vuoto.': 'Trash is empty.',
    'Chiudi KANT IDE': 'Close KANT IDE',
    'Sei sicuro di voler chiudere KANT IDE?': 'Are you sure you want to close KANT IDE?',
    'Ripristina sessione': 'Restore session',
    'La sessione precedente si è interrotta improvvisamente. Vuoi riprendere a lavorare da dove avevi lasciato?': 'The previous session ended unexpectedly. Resume where you left off?',
    'Revisione AI in sospeso': 'Pending AI review',
    'Chiudi senza applicare': 'Close without applying',
    'Conferma e applica': 'Confirm and apply',
    'Chiudi senza salvare': 'Close without saving',
    'Chiudi senza creare': 'Close without creating',
    'Chiudi senza creare nulla': 'Close without creating anything',
    'Chiudi senza fare nulla': 'Close without doing anything',
    'Annulla senza avviare': 'Cancel without starting',
    'Annulla senza fare commit': 'Cancel without committing',
    'Chiudi senza salvare le modifiche ai metadati': 'Close without saving metadata changes',
    'Salva tag, nome e descrizione': 'Save tag, name and description',

    # Home and project navigation
    'Apri cartella…': 'Open folder…',
    'Apri cartella': 'Open folder',
    'Scegli una cartella di progetto da aprire nell\'IDE': 'Choose a project folder to open in the IDE',
    '＋  Nuovo progetto': '＋  New project',
    'Crea un progetto nuovo da zero': 'Create a new project from scratch',
    'CARTELLE RECENTI': 'RECENT FOLDERS',
    'Apri questo progetto recente': 'Open this recent project',
    'Torna al menu iniziale': 'Back to home',
    '+  Nuovo file': '+  New file',
    '+  Nuovo gruppo': '+  New group',
    '+  Aggiungi un elemento': '+  Add an element',
    'Crea un nuovo file nella cartella del progetto': 'Create a new file in the project folder',
    'Raggruppa elementi da file diversi sotto un nome comune': 'Group elements from different files under one name',
    'Crea un nuovo modulo, classe, funzione o altro elemento in questo file': 'Create a module, class, function, or other element in this file',
    'Mostra la struttura concettuale del progetto come elementi KANT': 'Show the project conceptual structure as KANT elements',
    'Mostra cartelle e file del progetto senza la struttura concettuale KANT': 'Show project folders and files without the KANT structure',
    'Mostra i raggruppamenti del progetto (collezioni di elementi da file diversi) invece dell\'albero dei file': 'Show project groups instead of the file tree',
    'Gruppi': 'Groups',
    'Nuovo progetto': 'New project',
    'Nuovo file': 'New file',
    'Nuova cartella': 'New folder',
    'Nuovo elemento': 'New element',
    'Nuovo gruppo': 'New group',
    'Ripristina': 'Restore',
    'Rinomina': 'Rename',
    'Apri file': 'Open file',
    'File non testuale': 'Non-text file',
    'Apri prima una cartella di progetto.': 'Open a project folder first.',
    'Apri un progetto prima di modificare un import.': 'Open a project before editing an import.',
    'La modifica degli import è disponibile solo per file Python al momento.': 'Import editing is currently available only for Python files.',
    'Usa solo un nome, senza percorsi.': 'Use a name only, without paths.',
    'Usa solo un nome file, senza percorsi.': 'Use a file name only, without paths.',
    'Usa solo un nome cartella, senza percorsi.': 'Use a folder name only, without paths.',
    'Esiste già una cartella con questo nome.': 'A folder with this name already exists.',
    'Esiste gia un file o una cartella con questo nome.': 'A file or folder with this name already exists.',
    'Alcuni file sono cambiati durante la scansione. Ripeti la sostituzione.': 'Some files changed during the scan. Run the replacement again.',
    'Torna alla mappa completa': 'Back to full map',

    # Main toolbar, panels and editor
    'Salva (Ctrl+S)': 'Save (Ctrl+S)',
    'Annulla file (Ctrl+Z)': 'Undo file (Ctrl+Z)',
    'Ripeti file (Ctrl+Y)': 'Redo file (Ctrl+Y)',
    'Trova nel file (Ctrl+F)': 'Find in file (Ctrl+F)',
    'Esegui (Ctrl+R)': 'Run (Ctrl+R)',
    'Debug (F5)': 'Debug (F5)',
    'Cerca nel file aperto…': 'Search in the open file…',
    'Occorrenza precedente': 'Previous occurrence',
    'Occorrenza successiva (Invio)': 'Next occurrence (Enter)',
    'Chiudi la barra di ricerca (Esc)': 'Close search bar (Esc)',
    'Chiudi questo pannello': 'Close this panel',
    'Comprimi/espandi il terminale AI': 'Collapse/expand the AI terminal',
    'Riduci a icona': 'Minimize',
    'Massimizza/ripristina la finestra': 'Maximize/restore window',
    "Chiudi l'IDE": 'Close the IDE',
    'Cambia interprete Python per questo progetto': 'Change the Python interpreter for this project',
    'Elenca chi fa riferimento all\'elemento selezionato, e da dove': 'List what references the selected element and where from',
    'Elenca a cosa fa riferimento l\'elemento selezionato, e dove': 'List what the selected element references and where',
    'Mappa limitata all\'elemento (o modulo) attualmente aperto': 'Map limited to the currently open element or module',
    'Mappa completa del progetto': 'Full project map',
    'Apri la mappa grafica delle dipendenze del progetto': 'Open the project dependency map',
    'Apri/chiudi la mappa grafica delle dipendenze (MAPPA) del progetto': 'Open/close the project dependency map',
    'Controllo sintassi...': 'Checking syntax…',
    'Metadati KANT': 'KANT metadata',
    'Modifica metadati KANT': 'Edit KANT metadata',
    'Comprimi/espandi questa sezione': 'Collapse/expand this section',
    'Genera la struttura KANT (tag, nesting, #id) in modo deterministico, senza AI, per il file aperto nella plancia di coding.': 'Generate the KANT structure (tags, nesting, #id) deterministically, without AI, for the file open in the coding board.',
    "Chiedi all'AI (agente/modello/effort attualmente selezionati nella plancia AI) di compilare i campi CATEGORY e descrizione vuoti della convenzione KANT in quello che e' attualmente visualizzato nella plancia di coding (l'intero file, o solo l'elemento isolato — anche una foglia). Tag, nesting, marker OPEN/CLOSED e #id restano quelli già calcolati deterministicamente dall'IDE — l'AI scrive solo il testo.": 'Ask the AI agent/model/effort selected in the AI panel to fill empty KANT CATEGORY and description fields in what the coding board currently shows (the whole file or the isolated element, including a leaf). Tags, nesting, OPEN/CLOSED markers, and #id remain deterministic — AI writes text only.',
    'Mostra la struttura KANT come albero compatto con menu espandibili': 'Show the KANT structure as a compact expandable tree',

    # AI panel
    'Quale CLI AI usare per i messaggi inviati da questa plancia': 'Choose the AI CLI used for messages from this panel',
    'Modello per l\'agente selezionato': 'Model for the selected agent',
    'Reasoning effort per l\'agente selezionato': 'Reasoning effort for the selected agent',
    'Approva i permessi Claude; le modifiche restano soggette alla revisione finale.': 'Approve Claude permissions; changes still require final review.',
    "Se disattivo (default), i messaggi in chat AI includono un riferimento nascosto al file/elemento attualmente aperto nella plancia di coding, cosi le modifiche restano mirate a quel punto. Attiva GLOBAL per far considerare all'AI l'intero progetto invece di un file/elemento specifico.": 'When off (default), AI messages include a hidden reference to the file or element open in the coding board, keeping changes focused there. Enable GLOBAL to use the entire project instead.',
    'Chiedi, modifica o analizza il codice… (Invio invia · Ctrl+Invio va a capo)': 'Ask, edit, or analyze code… (Enter sends · Ctrl+Enter adds a line)',
    'Allega documenti o immagini da far leggere a Claude/Codex': 'Attach documents or images for Claude/Codex to read',
    "Modalità risparmio token (lossy) — attiva/disattiva: le immagini allegate vengono ridimensionate e ricompresse prima dell'invio, per far leggere meno token al modello a scapito della qualità. I documenti (PDF, DOCX, ...) non sono affetti da questa opzione.": 'Token-saving mode (lossy): attached images are resized and recompressed before sending, reducing model token use at the cost of quality. Documents (PDF, DOCX, ...) are unaffected.',
    'Conversazioni a sinistra, AI al centro e plancia KANT a destra': 'Chats on the left, AI in the center, and KANT board on the right',
    'Invia il messaggio (Invio); se un comando è in corso, lo interrompe': 'Send the message (Enter); stop the current command if one is running',
    'Claude è in attesa della tua scelta.': 'Claude is waiting for your choice.',
    'Allegato ridotto prima dell\'invio (documento convertito o immagine compressa)': 'Attachment reduced before sending (converted document or compressed image)',
    'Rimuovi questo allegato': 'Remove this attachment',

    # File menu
    'Comandi sul file attivo: salva, annulla/ripeti, esegui, esegui test': 'Commands for the active file: save, undo/redo, run, run tests',
    'Salva il file attivo su disco (Ctrl+S)': 'Save the active file to disk (Ctrl+S)',
    'Annulla file': 'Undo file',
    'Annulla l\'ultima modifica al file attivo (Ctrl+Z)': 'Undo the last change to the active file (Ctrl+Z)',
    'Ripeti file': 'Redo file',
    'Ripristina la modifica appena annullata (Ctrl+Y)': 'Redo the last undone change (Ctrl+Y)',
    'Esegui': 'Run',
    'Esegue il file attivo con l\'interprete/comando adatto al suo tipo (Ctrl+R)': 'Run the active file with the appropriate interpreter or command (Ctrl+R)',
    'Esegui test (Ctrl+Shift+T)': 'Run tests (Ctrl+Shift+T)',
    'Esegue l\'intera suite pytest del progetto e mostra i risultati': 'Run the full project pytest suite and show the results',

    # KANT menu
    'Verifica e generazione della struttura KANT': 'Validate and generate the KANT structure',
    'Verifica KANT': 'Validate KANT',
    'Controlla che i marcatori KANT (tag/#id, apertura/chiusura) di tutto il progetto siano validi': 'Check that all project KANT markers (tag/#id, open/close) are valid',
    'Genera struttura (file corrente)': 'Generate structure (current file)',
    'AI KANT Comment (intero file)': 'AI KANT Comment (whole file)',
    'AI KANT Comment (intero progetto)': 'AI KANT Comment (whole project)',
    'Aggiunge deterministicamente (tag/nesting/#id, senza AI) i marker mancanti nel file attivo — le stesse regole del tasto ✨ nella barra azioni in modalità File': 'Deterministically add missing markers (tag/nesting/#id, without AI) to the active file — the same rules as the ✨ action in File mode',
    'Genera la struttura mancante e chiede all’AI di compilare i commenti KANT dell’intero file attivo, anche quando nella plancia è isolato un solo blocco': 'Generate missing structure and ask AI to fill KANT comments for the whole active file, even when the board isolates one block',
    'Controlla ricorsivamente tutti i sorgenti supportati nella cartella del progetto, genera deterministicamente la struttura mancante e chiede all’AI soltanto le descrizioni': 'Recursively check all supported source files, generate missing structure deterministically, and ask AI only for descriptions',
    'Rimuovi tutti i commenti KANT (progetto)': 'Remove all KANT comments (project)',
    'Rimuovi e rigenera tutto (deterministico)': 'Remove and regenerate everything (deterministic)',
    'ATTENZIONE: elimina tutti e soli i marker/commenti KANT da ogni file; codice e commenti normali restano': 'WARNING: remove only KANT markers/comments from every file; code and normal comments remain',
    'ATTENZIONE: rimuove ogni marker KANT (incluse le descrizioni) da tutto il progetto e ricrea la struttura da zero in modo deterministico — le descrizioni andranno riscritte': 'WARNING: remove every KANT marker (including descriptions) from the whole project and rebuild the structure deterministically — descriptions must be rewritten',
    'Rimuovi tutti i commenti KANT': 'Remove all KANT comments',
    'Rimuovi e rigenera struttura KANT': 'Remove and regenerate KANT structure',
    'Convenzione KANT': 'KANT convention',
    'Scheletro KANT': 'KANT skeleton',
    'Errore KANT': 'KANT error',
    'Marcatori KANT non validi': 'Invalid KANT markers',
    'Struttura KANT generata deterministicamente per questo file.': 'KANT structure generated deterministically for this file.',
    'Nessun elemento da taggare in questo file (già completo, o linguaggio non supportato).': 'No elements to tag in this file (already complete or unsupported language).',
    'Impossibile avviare il processo AI in questo momento.': 'The AI process cannot be started right now.',
    'Questo file ha marker KANT non validi: esegui prima "Verifica KANT" dal menu File.': 'This file has invalid KANT markers: run "Validate KANT" from the File menu first.',

    # Search, appearance and LSP menus
    'Cerca': 'Search',
    'Trova e sostituisci testo nel file attivo o in tutto il progetto': 'Find and replace text in the active file or the whole project',
    'Trova nel file': 'Find in file',
    'Cerca (ed eventualmente sostituisce) del testo nel file attualmente aperto': 'Find and optionally replace text in the open file',
    'Cerca nel progetto': 'Search in project',
    'Cerca del testo in tutti i file del progetto aperto': 'Search text in every file of the open project',
    'Sostituisci nel progetto': 'Replace in project',
    'Cerca e sostituisce del testo in tutti i file del progetto aperto': 'Find and replace text in every file of the open project',
    'Aspetto': 'Appearance',
    'Tema chiaro/scuro e la palette comandi': 'Light/dark theme and command palette',
    'Notte': 'Dark',
    'Giorno': 'Light',
    'Passa dal tema chiaro a quello scuro (o viceversa)': 'Switch between light and dark themes',
    'Palette comandi (Ctrl+Shift+P)': 'Command palette (Ctrl+Shift+P)',
    'Apre un elenco cercabile di tutti i comandi disponibili': 'Open a searchable list of all available commands',
    'Modalità VIM': 'VIM mode',
    'Editing modale stile VIM nei blocchi di codice: Normal/Insert/Visual, motion h/j/k/l/w/b/e, operatori d/y/c, navigazione strutturale j/k/gg/G tra gli elementi, za per piegare, / e : per cercare ed eseguire comandi. Disattivala per digitare sempre normalmente, come prima.': 'VIM-style modal editing in code blocks: Normal/Insert/Visual, h/j/k/l/w/b/e motions, d/y/c operators, j/k/gg/G structural navigation, za folding, / search, and : commands. Disable it for normal typing.',
    'Funzioni del language server: hover, definizione, rename, formattazione, lint, dipendenze': 'Language server features: hover, definition, rename, formatting, lint, dependencies',
    'Hover (o passa il mouse su un simbolo)': 'Hover (or point at a symbol)',
    'Mostra le informazioni del language server per il simbolo sotto il cursore': 'Show language server information for the symbol under the cursor',
    'Vai alla definizione (Ctrl+Click)': 'Go to definition (Ctrl+Click)',
    'Salta alla definizione del simbolo sotto il cursore': 'Jump to the definition of the symbol under the cursor',
    'Elenca tutti i punti del progetto che usano il simbolo sotto il cursore': 'List every project location using the symbol under the cursor',
    'Rinomina il simbolo sotto il cursore in tutto il progetto': 'Rename the symbol under the cursor across the project',
    'Formatta documento': 'Format document',
    'Formatta il file attivo tramite il language server configurato': 'Format the active file using the configured language server',
    'Formatta con black/ruff': 'Format with black/ruff',
    "Formatta il file Python attivo con black o ruff (dell'interprete del progetto, o del PATH di sistema)": 'Format the active Python file with black or ruff from the project interpreter or system PATH',
    'Installa dipendenze': 'Install dependencies',
    'Installa le dipendenze da requirements.txt o pyproject.toml nell\'interprete del progetto': 'Install dependencies from requirements.txt or pyproject.toml in the project interpreter',
    'Esegui lint (ruff/flake8)': 'Run lint (ruff/flake8)',
    'Analizza il progetto con ruff o flake8 e mostra i problemi trovati': 'Analyze the project with ruff or flake8 and show found issues',

    # Git
    'Aggiorna lo stato Git mostrato nella barra e nella struttura del progetto': 'Refresh Git status in the bar and project tree',
    'Diff file': 'Diff file',
    'Mostra le differenze non salvate del file attivo rispetto a Git': 'Show active-file changes compared with Git',
    'Aggiunge il file attivo alla staging area (git add)': 'Add the active file to the staging area (git add)',
    'Rimuove il file attivo dalla staging area (git reset)': 'Remove the active file from the staging area (git reset)',
    'Crea un commit con i file attualmente in staging': 'Create a commit from currently staged files',
    'Cambia branch...': 'Switch branch…',
    'Cambia il branch Git attivo per questo progetto': 'Switch the active Git branch for this project',
    'Altro...': 'More…',
    'Apri il pannello Git completo (branch/stage/diff/commit assieme, o il flusso git-init se il progetto non ha ancora un repo)': 'Open the full Git panel (branch/stage/diff/commit together, or git init when the project has no repository)',
    'Branch:': 'Branch:',
    'File modificati (spunta = in stage):': 'Changed files (checked = staged):',
    'Seleziona un file per vedere il diff': 'Select a file to view its diff',
    'Messaggio di commit:': 'Commit message:',
    'Scrivi un messaggio di commit prima.': 'Write a commit message first.',
    'Nessun file in stage.': 'No staged files.',

    # Forms and dialogs
    'Tag:': 'Tag:',
    'Nome tecnico:': 'Technical name:',
    'Descrizione breve:': 'Short description:',
    'Tipo di elemento:': 'Element type:',
    'Tipo di file:': 'File type:',
    'Linguaggio (determina la sintassi generata):': 'Language (determines generated syntax):',
    'Linguaggio (determina sintassi/estensione consigliata):': 'Language (determines syntax/recommended extension):',
    'Nome del file:': 'File name:',
    'Nome del gruppo:': 'Group name:',
    'Nome del progetto:': 'Project name:',
    'Cartella principale (il progetto verrà creato al suo interno):': 'Parent folder (the project will be created inside it):',
    'Cartella principale': 'Parent folder',
    'Linguaggio principale (determina il modulo di esempio):': 'Primary language (determines the example module):',
    'Crea un modulo di esempio con tag KANT': 'Create an example module with KANT tags',
    'es. calcola_totale': 'e.g. calculate_total',
    'cosa fa questo elemento, in poche parole': 'what this element does, in a few words',
    'es. Autenticazione': 'e.g. Authentication',
    'es. shop-backend': 'e.g. shop-backend',
    'Filtra comandi…': 'Filter commands…',
    'Filtra per tag, nome o file…': 'Filter by tag, name, or file…',
    'Scegli l\'interprete/venv per questo progetto:': 'Choose the interpreter/venv for this project:',
    'Interprete Python': 'Python interpreter',
    "Scegli l'eseguibile Python": 'Choose the Python executable',

    # MAPPA
    'Hover · clicca l’arco per fissare': 'Hover · click an edge to pin it',
    'Esce dalla vista concentrata su un elemento e torna alla mappa completa': 'Exit the focused view and return to the full map',
    'Cerca per nome o descrizione…': 'Search by name or description…',
    'Filtra i nodi mostrati per nome o descrizione': 'Filter displayed nodes by name or description',
    'Mostra solo i nodi del file scelto (o tutti i file)': 'Show nodes from the selected file only (or all files)',
    'Espandi tutti': 'Expand all',
    'Espande tutti i moduli/classi raggruppati nella mappa': 'Expand every grouped module/class in the map',
    'Comprimi tutti': 'Collapse all',
    'Comprime tutti i moduli/classi in un singolo nodo ciascuno': 'Collapse every module/class into a single node',
    'Riorganizza': 'Rearrange',
    'Ricalcola la disposizione dei nodi, scartando le posizioni trascinate a mano': 'Recalculate node layout and discard manually dragged positions',
    'Isola selezionato': 'Isolate selected',
    'Apre la visualizzazione LOCAL dell\'elemento selezionato': 'Open the LOCAL view of the selected element',
    'Colora i nodi per connettività (caldo = molti riferimenti) invece che per tag': 'Color nodes by connectivity (hot = many references) instead of tag',
    'Direzione: Sx → Dx': 'Direction: Left → Right',
    'Direzione: Dx → Sx': 'Direction: Right → Left',
    'Inverte la direzione del flusso logico del codice nella mappa': 'Reverse the direction of logical code flow in the map',
    'Esci dalla MAPPA': 'Exit MAP',
    'Chiude la MAPPA e torna alla plancia di coding': 'Close MAP and return to the coding board',
    'File:': 'File:',
    'Tag:': 'Tag:',
    'Connessioni:': 'Connections:',
    'Appartenenza': 'Membership',
    'Connessione neutra che collega un elemento alla sua origine comune (modulo/classe)': 'Neutral link connecting an element to its common origin (module/class)',
    'Origine comune (elemento radice non visualizzato)': 'Common origin (root element hidden)',
    'Trascina i nodi per disporli · doppio clic per espandere o aprire · Riorganizza ripristina il layout': 'Drag nodes to arrange · double-click to expand or open · Rearrange resets the layout',

    # Context menus and status messages
    'Nuovo file…': 'New file…',
    'Nuova cartella…': 'New folder…',
    'Ripristina dal cestino...': 'Restore from trash…',
    'Visualizza in Esplora risorse': 'Show in File Explorer',
    'Elimina elemento KANT': 'Delete KANT element',
    'Elimina questo elemento KANT e tutto il codice contenuto': 'Delete this KANT element and all contained code',
    'Eliminare "{name}" e tutto il codice contenuto, inclusi gli elementi figli?\n\nPuoi annullare con Ctrl+Z.': 'Delete "{name}" and all contained code, including child elements?\n\nYou can undo with Ctrl+Z.',
    "L'elemento non e piu presente nel file.": 'The element is no longer in the file.',
    "Impossibile salvare la modifica: l'elemento non e stato eliminato.": 'Could not save the change: the element was not deleted.',
    'Copia nome elemento': 'Copy element name',
    'Copia percorso file': 'Copy file path',
    'Aggiungi a un gruppo': 'Add to group',
    'Aggiungi a un nuovo gruppo…': 'Add to a new group…',
    'Copia file': 'Copy files',
    'Modifica import': 'Edit import',
    'Dipendenze': 'Dependencies',
    'Formatta': 'Format',
    'Apri un file prima di usare i comandi LSP.': 'Open a file before using LSP commands.',
    'Metti il cursore dentro un blocco di codice.': 'Place the cursor inside a code block.',
    'Server LSP non pronto.': 'LSP server is not ready.',
    'Metti il cursore sopra un simbolo.': 'Place the cursor over a symbol.',
    'Nessuna occorrenza nel file aperto.': 'No occurrences in the open file.',
    'Il file e gia pulito.': 'The file is already clean.',
    'Formattazione black/ruff disponibile solo per file Python.': 'black/ruff formatting is available only for Python files.',
    'Ne black ne ruff sono installati (pip install black, oppure pip install ruff).': 'Neither black nor ruff is installed (pip install black or pip install ruff).',
    'Il file e gia formattato.': 'The file is already formatted.',
    'Nessun requirements.txt o pyproject.toml trovato in questo progetto.': 'No requirements.txt or pyproject.toml was found in this project.',
    'Nessun comando run configurato per questo tipo di file.': 'No run command is configured for this file type.',
    'Il debug e disponibile solo per file Python in questa versione.': 'Debugging is available only for Python files in this version.',
    'Permesso rifiutato': 'Permission denied',
    'ARCHIVIO CHAT': 'CHAT ARCHIVE',
    '+ Nuova conversazione': '+ New conversation',
    'Raggruppa': 'Group',
    'Raggruppa la chat selezionata; un nome vuoto la rimuove dal gruppo': 'Group the selected chat; an empty name removes it from its group',
    'Riferimenti': 'References',
    'Rinomina simbolo (F2)': 'Rename symbol (F2)',
    'Rifiuta': 'Deny',
    'Consenti una volta': 'Allow once',
    'Consenti per la sessione': 'Allow for session',
    'Rifiutato': 'Denied',
    'Consentito una volta': 'Allowed once',
    'Consentito per la sessione': 'Allowed for session',
    'Consentito automaticamente': 'Allowed automatically',
    'Claude è in attesa della tua scelta.': 'Claude is waiting for your choice.',
    'Nega questa singola richiesta di permesso': 'Deny this permission request',
    'Consenti solo questa singola richiesta, ne verra richiesto di nuovo la prossima volta': 'Allow this request once; ask again next time',
    'Consenti questo tipo di richiesta per il resto della sessione, senza chiedere ogni volta': 'Allow this request type for the rest of the session without asking again',
    'Accetta': 'Accept',
    'Rifiuta modifiche': 'Reject changes',
    'Allega file': 'Attach files',
    'Immagini e documenti (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.svg *.pdf *.txt *.md *.csv *.json);;Tutti i file (*)': 'Images and documents (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.svg *.pdf *.txt *.md *.csv *.json);;All files (*)',
}

EN_TO_IT = {english: italian for italian, english in IT_TO_EN.items()}
CURRENT_LANGUAGE = 'en'
_APP_LANGUAGE_FILTER = None


def current_language():
    return CURRENT_LANGUAGE


def translate_text(text, language=None):
    """Translate one exact UI string in either direction, leaving user content untouched."""
    if not text:
        return text
    language = language or CURRENT_LANGUAGE
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    core = text.strip()
    translated = (IT_TO_EN if language == 'en' else EN_TO_IT).get(core)
    return f'{leading}{translated}{trailing}' if translated is not None else text


def _translate_object(obj, language):
    if isinstance(obj, QAction):
        text = translate_text(obj.text(), language)
        tooltip = translate_text(obj.toolTip(), language)
        status_tip = translate_text(obj.statusTip(), language)
        if text != obj.text():
            obj.setText(text)
        if tooltip != obj.toolTip():
            obj.setToolTip(tooltip)
        if status_tip != obj.statusTip():
            obj.setStatusTip(status_tip)
        return
    if isinstance(obj, QWidget):
        tooltip = translate_text(obj.toolTip(), language)
        if tooltip != obj.toolTip():
            obj.setToolTip(tooltip)
        if isinstance(obj, QDialog):
            title = translate_text(obj.windowTitle(), language)
            if title != obj.windowTitle():
                obj.setWindowTitle(title)
        if isinstance(obj, QAbstractButton):
            text = translate_text(obj.text(), language)
            if text != obj.text():
                obj.setText(text)
        elif isinstance(obj, QLabel):
            text = translate_text(obj.text(), language)
            if text != obj.text():
                obj.setText(text)
        elif isinstance(obj, QGroupBox):
            title = translate_text(obj.title(), language)
            if title != obj.title():
                obj.setTitle(title)
        if isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit)):
            placeholder = translate_text(obj.placeholderText(), language)
            if placeholder != obj.placeholderText():
                obj.setPlaceholderText(placeholder)
        if isinstance(obj, QComboBox):
            for index in range(obj.count()):
                text = translate_text(obj.itemText(index), language)
                if text != obj.itemText(index):
                    obj.setItemText(index, text)
        if isinstance(obj, QTabWidget):
            for index in range(obj.count()):
                text = translate_text(obj.tabText(index), language)
                if text != obj.tabText(index):
                    obj.setTabText(index, text)
        if isinstance(obj, QMenu):
            title = translate_text(obj.title(), language)
            if title != obj.title():
                obj.setTitle(title)


class UiLanguage(QObject):
    """Translate the existing window and any menu/dialog shown later."""
    def __init__(self, language, parent=None):
        super().__init__(parent)
        self.set_language(language)

    def set_language(self, language):
        global CURRENT_LANGUAGE
        self.language = 'it' if str(language).lower().startswith('it') else 'en'
        CURRENT_LANGUAGE = self.language

    def apply(self, root):
        _translate_object(root, self.language)
        for obj in root.findChildren(QObject):
            _translate_object(obj, self.language)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show:
            _translate_object(obj, self.language)
            if isinstance(obj, QMenu):
                for action in obj.actions():
                    _translate_object(action, self.language)
        return False


def install_ui_language(app, language):
    """Return the application's one language filter, updating its active language."""
    global _APP_LANGUAGE_FILTER
    if _APP_LANGUAGE_FILTER is None:
        _APP_LANGUAGE_FILTER = UiLanguage(language, app)
        app.installEventFilter(_APP_LANGUAGE_FILTER)
    else:
        _APP_LANGUAGE_FILTER.set_language(language)
    return _APP_LANGUAGE_FILTER
