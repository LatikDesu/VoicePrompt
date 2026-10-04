"""Test-only OpenAI-compatible local endpoint; never distributed with the plugin."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path != '/v1/models':
            self.send_error(404)
            return
        content = json.dumps({'data': [{'id': 'local-test'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_error(404)
            return
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size < 65536:
            self.send_error(413)
            return
        try:
            value = json.loads(self.rfile.read(size))
        except ValueError:
            self.send_error(400)
            return
        if (value.get('model') != 'local-test' or value.get('stream') is not False
                or len(value.get('messages', [])) != 2
                or value['messages'][0].get('role') != 'system'
                or value['messages'][1] != {'role': 'user', 'content': 'Не удаляй 42 файла'}):
            self.send_error(422)
            return
        content = json.dumps({'choices': [{'message': {'content': 'Не удаляй сорок два файла.'}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 18765), Handler)
    print('READY localhost:18765', flush=True)
    server.serve_forever(poll_interval=0.2)
