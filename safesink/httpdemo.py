from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, threading

class State:
    def __init__(self): self.keys=set(); self.events=[]

class Handler(BaseHTTPRequestHandler):
    state=State()
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0')); body=self.rfile.read(n)
        key=self.headers.get('Idempotency-Key') or json.loads(body).get('effect_key')
        accepted=key not in self.state.keys
        if accepted: self.state.keys.add(key); self.state.events.append(json.loads(body))
        self.send_response(201 if accepted else 200); self.end_headers(); self.wfile.write(json.dumps({'accepted':accepted}).encode())
    def log_message(self,*args): pass

def start(port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler); t=threading.Thread(target=server.serve_forever,daemon=True); t.start(); return server
