"""Minimal stdio MCP server used by Claude Code to ask KANT IDE for permission."""
import json
import os
import socket
import sys


TOOL = {
    'name': 'approve',
    'description': 'Ask the KANT IDE user whether Claude may invoke a tool.',
    'inputSchema': {
        'type': 'object',
        'properties': {'tool_name': {'type': 'string'}, 'input': {'type': 'object'}},
        'required': ['tool_name', 'input'],
        'additionalProperties': True,
    },
}


def ask_ide(arguments):
    request = {
        'token': os.environ.get('KANT_PERMISSION_TOKEN', ''),
        'tool_name': arguments.get('tool_name', ''),
        'input': arguments.get('input', {}),
    }
    try:
        with socket.create_connection(('127.0.0.1', int(os.environ['KANT_PERMISSION_PORT'])), timeout=610) as connection:
            connection.sendall((json.dumps(request) + '\n').encode('utf-8'))
            response = connection.makefile('rb').readline(1_000_001)
        return json.loads(response.decode('utf-8'))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {'behavior': 'deny', 'message': f'KANT IDE non ha risposto: {error}'}


def handle_message(message, ask=ask_ide):
    if 'id' not in message:
        return None
    request_id, method = message['id'], message.get('method')
    if method == 'initialize':
        version = message.get('params', {}).get('protocolVersion', '2025-06-18')
        result = {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'kant-permissions', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call':
        params = message.get('params', {})
        if params.get('name') != 'approve':
            return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'Tool sconosciuto'}}
        decision = ask(params.get('arguments', {}))
        result = {'content': [{'type': 'text', 'text': json.dumps(decision, ensure_ascii=False)}]}
    else:
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Metodo non supportato'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    for raw in sys.stdin.buffer:
        try:
            incoming = json.loads(raw)
            messages = incoming if isinstance(incoming, list) else [incoming]
            replies = [reply for message in messages if (reply := handle_message(message)) is not None]
            if replies:
                output = replies if isinstance(incoming, list) else replies[0]
                sys.stdout.write(json.dumps(output, ensure_ascii=False) + '\n')
                sys.stdout.flush()
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            sys.stderr.write(f'KANT permission MCP: {error}\n')


if __name__ == '__main__':
    main()
