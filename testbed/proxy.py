import json, time, requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UP = "http://127.0.0.1:11434"
LOG = open("logs/wire.jsonl", "a", buffering=1)

def note(**kw):
    LOG.write(json.dumps({"t": time.time_ns(), **kw}) + "\n")

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def do_GET(self):
        r = requests.get(UP + self.path)
        self.send_response(r.status_code)
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try: req = json.loads(body)
        except Exception: req = body.decode(errors="replace")
        note(dir="req", path=self.path, body=req)
        r = requests.post(UP + self.path, data=body, stream=True,
                          headers={"Content-Type": "application/json"})
        self.send_response(r.status_code)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = []
        for c in r.iter_content(chunk_size=None):
            if not c: continue
            chunks.append(c)
            self.wfile.write(b"%x\r\n" % len(c) + c + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        note(dir="resp", status=r.status_code, body=b"".join(chunks).decode(errors="replace"))

ThreadingHTTPServer(("127.0.0.1", 11435), H).serve_forever()
