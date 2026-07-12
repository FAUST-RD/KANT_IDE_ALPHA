"""Authenticated localhost bridge for Claude Code permission prompts."""
import json
import os
import secrets
import socket
import sys
import tempfile
import threading

from PySide6.QtCore import QObject, Signal


class PermissionBridge(QObject):
    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.token = secrets.token_urlsafe(24)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(('127.0.0.1', 0))
        self._socket.listen()
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._pending = []
        threading.Thread(target=self._serve, daemon=True, name='kant-ai-permissions').start()

    def _serve(self):
        while not self._stopped.is_set():
            try:
                connection, _address = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        request = None
        try:
            with connection:
                line = connection.makefile('rb').readline(1_000_001)
                data = json.loads(line.decode('utf-8')) if line and len(line) <= 1_000_000 else {}
                if not secrets.compare_digest(str(data.get('token', '')), self.token):
                    raise ValueError('token non valido')
                request = {
                    'tool_name': str(data.get('tool_name', '')),
                    'input': data.get('input') if isinstance(data.get('input'), dict) else {},
                    'event': threading.Event(),
                    'response': None,
                }
                with self._lock:
                    self._pending.append(request)
                self.requested.emit(request)
                request['event'].wait(600)
                response = request['response'] or {'behavior': 'deny', 'message': 'Richiesta scaduta o IDE chiuso'}
                connection.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if request:
                with self._lock:
                    if request in self._pending:
                        self._pending.remove(request)

    def resolve(self, request, allow, message='Permesso rifiutato'):
        if request['event'].is_set():
            return
        request['response'] = (
            {'behavior': 'allow', 'updatedInput': request['input']}
            if allow else {'behavior': 'deny', 'message': message}
        )
        request['event'].set()

    def resolve_all(self, allow, message='Operazione interrotta'):
        with self._lock:
            pending = list(self._pending)
        for request in pending:
            self.resolve(request, allow, message)

    def stop(self):
        self.resolve_all(False, 'IDE chiuso')
        self._stopped.set()
        try:
            self._socket.close()
        except OSError:
            pass


def write_permission_config(bridge):
    helper = os.path.join(os.path.dirname(__file__), 'permission_mcp.py')
    config = {
        'mcpServers': {
            'kant_permissions': {
                'command': sys.executable,
                'args': [helper],
                'env': {
                    'KANT_PERMISSION_PORT': str(bridge.port),
                    'KANT_PERMISSION_TOKEN': bridge.token,
                },
            },
        },
    }
    fd, path = tempfile.mkstemp(prefix='.kant-ai-permissions-', suffix='.json')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        json.dump(config, stream)
    return path
