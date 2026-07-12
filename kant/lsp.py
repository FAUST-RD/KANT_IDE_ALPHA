"""LSP client and per-language server configuration."""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QObject, QProcess, Signal


LSP_SERVERS_BY_EXT = {
    '.py': ('pyright-langserver', 'pylsp'),
    '.js': ('typescript-language-server',),
    '.jsx': ('typescript-language-server',),
    '.ts': ('typescript-language-server',),
    '.tsx': ('typescript-language-server',),
    '.go': ('gopls',),
    '.rs': ('rust-analyzer',),
    '.c': ('clangd',),
    '.h': ('clangd',),
    '.cpp': ('clangd',),
    '.cc': ('clangd',),
    '.cxx': ('clangd',),
    '.hpp': ('clangd',),
    '.java': ('jdtls',),
    '.php': ('intelephense',),
    '.rb': ('solargraph',),
}

LSP_SERVER_ARGS = {
    'pyright-langserver': ['--stdio'],
    'typescript-language-server': ['--stdio'],
    'gopls': ['serve'],
    'intelephense': ['--stdio'],
    'solargraph': ['stdio'],
}

LSP_LANGUAGE_BY_EXT = {
    '.py': 'python',
    '.js': 'javascript',
    '.jsx': 'javascriptreact',
    '.ts': 'typescript',
    '.tsx': 'typescriptreact',
    '.go': 'go',
    '.rs': 'rust',
    '.c': 'c',
    '.h': 'c',
    '.cpp': 'cpp',
    '.cc': 'cpp',
    '.cxx': 'cpp',
    '.hpp': 'cpp',
    '.java': 'java',
    '.php': 'php',
    '.rb': 'ruby',
}


def lsp_server_for_path(path):
    for name in LSP_SERVERS_BY_EXT.get(Path(path).suffix.lower(), ()):
        if shutil.which(name):
            return name
    return None


def lsp_config_for_path(path):
    name = lsp_server_for_path(path)
    if not name:
        return None
    return shutil.which(name), LSP_SERVER_ARGS.get(name, []), name, LSP_LANGUAGE_BY_EXT.get(Path(path).suffix.lower(), 'plaintext')


def file_uri(path):
    return Path(path).resolve().as_uri()


def path_from_file_uri(uri):
    parsed = urlparse(uri)
    if parsed.scheme != 'file':
        return uri
    path = unquote(parsed.path)
    if os.name == 'nt' and path.startswith('/') and len(path) > 2 and path[2] == ':':
        path = path[1:]
    return os.path.abspath(path)


class LspClient(QObject):
    diagnosticsChanged = Signal(str, list)
    responseReceived = Signal(int, str, object)
    serverError = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.server_name = None
        self.root = None
        self.ready = False
        self.next_id = 1
        self.init_id = None
        self.buffer = b''
        self.opened = {}
        self.pending = []
        self.requests = {}

    def shutdown(self):
        process, self.process = self.process, None
        if process is not None:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()
        self.server_name = None
        self.root = None
        self.ready = False
        self.init_id = None
        self.buffer = b''
        self.opened.clear()
        self.pending.clear()
        self.requests.clear()

    def update_document(self, root, path, text):
        config = lsp_config_for_path(path)
        if not config:
            self.shutdown()
            return False
        executable, args, server_name, language_id = config
        root = os.path.abspath(root or os.path.dirname(path) or os.getcwd())
        if self.process is None or self.server_name != server_name or self.root != root:
            self._start(executable, args, server_name, root)
        if self.process is None:
            return False
        self._send_document(path, language_id, text)
        return True

    def _start(self, executable, args, server_name, root):
        self.shutdown()
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self.process.setProgram(executable)
        self.process.setArguments(args)
        self.process.setWorkingDirectory(root)
        self.process.start()
        if not self.process.waitForStarted(1500):
            self.shutdown()
            return False
        self.server_name = server_name
        self.root = root
        self.init_id = self._send_request('initialize', {
            'processId': os.getpid(),
            'rootUri': file_uri(root),
            'capabilities': {'textDocument': {
                'publishDiagnostics': {'relatedInformation': False},
                'hover': {},
                'definition': {},
                'references': {},
                'rename': {},
                'formatting': {},
            }},
        })
        return True

    def _read_stderr(self):
        if self.process is not None:
            self.process.readAllStandardError()  # drain the pipe; servers often log verbosely here

    def _process_error(self, _error):
        if self.process is not None:
            self.serverError.emit(self.process.errorString())
        self.shutdown()

    def _process_finished(self, _exit_code, _status):
        if self.process is not None:
            self.serverError.emit(f'{self.server_name or "LSP"} terminato')
        self.shutdown()

    def close_document(self, path):
        uri = file_uri(path)
        if uri in self.opened and self.ready:
            self._send_notification('textDocument/didClose', {'textDocument': {'uri': uri}})
        self.opened.pop(uri, None)

    def _send_document(self, path, language_id, text):
        if not self.process:
            return
        job = (path, language_id, text)
        if not self.ready:
            self.pending.append(job)
            return
        self._send_document_now(*job)

    def _send_document_now(self, path, language_id, text):
        uri = file_uri(path)
        version = self.opened.get(uri, 0) + 1
        self.opened[uri] = version
        if version == 1:
            self._send_notification('textDocument/didOpen', {
                'textDocument': {'uri': uri, 'languageId': language_id, 'version': version, 'text': text},
            })
        else:
            self._send_notification('textDocument/didChange', {
                'textDocument': {'uri': uri, 'version': version},
                'contentChanges': [{'text': text}],
            })

    def _send_request(self, method, params):
        msg_id = self.next_id
        self.next_id += 1
        self.requests[msg_id] = method
        self._send({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params})
        return msg_id

    def request(self, method, params):
        if not self.process or not self.ready:
            return None
        return self._send_request(method, params)

    def _send_notification(self, method, params):
        self._send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def _send(self, payload):
        if not self.process:
            return
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        self.process.write(f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii') + body)

    def _read_stdout(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while True:
            header_end = self.buffer.find(b'\r\n\r\n')
            if header_end == -1:
                return
            headers = self.buffer[:header_end].decode('ascii', errors='replace').split('\r\n')
            length = 0
            for header in headers:
                if header.lower().startswith('content-length:'):
                    length = int(header.split(':', 1)[1].strip())
                    break
            start = header_end + 4
            if len(self.buffer) < start + length:
                return
            raw = self.buffer[start:start + length]
            self.buffer = self.buffer[start + length:]
            try:
                self._handle_message(json.loads(raw.decode('utf-8')))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue

    def _handle_message(self, message):
        if message.get('id') == self.init_id:
            self.requests.pop(message.get('id'), None)
            if message.get('error'):
                self.serverError.emit(str(message['error'].get('message', 'initialize fallito')))
                self.shutdown()
                return
            self.ready = True
            self._send_notification('initialized', {})
            pending, self.pending = self.pending, []
            for job in pending:
                self._send_document_now(*job)
            return
        if 'id' in message and 'method' in message:
            self._send({'jsonrpc': '2.0', 'id': message['id'], 'result': None})
            return
        if 'id' in message:
            method = self.requests.pop(message.get('id'), '')
            if method:
                self.responseReceived.emit(message.get('id'), method, message.get('result'))
            return
        if message.get('method') != 'textDocument/publishDiagnostics':
            return
        params = message.get('params', {})
        self.diagnosticsChanged.emit(path_from_file_uri(params.get('uri', '')), params.get('diagnostics', []))

