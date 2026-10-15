#!/usr/bin/env python3
"""Capture the OAuth redirect code automatically.

Run this, then open the --print-url URL in your browser (viewing account),
approve, and the redirect to http://localhost/<CODE> is caught here.
The code is exchanged for a token and saved to viewer_token.json.

Usage:  python oauth_listen.py        (Ctrl-C to stop)
"""
import http.server, sys, os, time, urllib.parse, subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import env  # loads .env (VIEWER_CLIENT_ID)

PORT = int(os.environ.get("OAUTH_PORT", "80"))
# Your OWN OAuth client ID (Google Cloud Console → Credentials → Desktop app).
CID = os.environ.get("VIEWER_CLIENT_ID", "PASTE-YOUR-OAUTH-CLIENT-ID.apps.googleusercontent.com")
AUTH_URL = (
    "https://accounts.google.com/o/oauth2/auth?"
    + "client_id=" + CID
    + "&redirect_uri=http%3A%2F%2Flocalhost"
    + "&response_type=code"
    + "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube.readonly"
    + "&access_type=offline"
    + "&prompt=consent"
)

AUTH_PAGE = b"""<!doctype html><meta charset=utf-8>
<meta http-equiv="refresh" content="3;url=__URL__">
<title>YouTube viewer OAuth</title>
<body style="font-family:sans-serif;max-width:40em;margin:4em auto">
<h2>YouTube viewer account OAuth</h2>
<p>Redirecting in 3s&hellip; if nothing happens, <a href="__URL__">click here</a>.</p>
<p>Log in with the <b>VIEWING</b> account, approve, then the &ldquo;can&rsquo;t reach this
page&rdquo; error on <code>http://localhost/4/...</code> is EXPECTED &mdash; the code is
captured automatically.</p></body></html>
""".replace(b"__URL__", AUTH_URL.encode())


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/4/") or "/4/" in self.path:
            code = self.path.split("/4/", 1)[-1].split("?")[0]
            body = ("OK - captured auth code. You can close this tab and stop the listener."
                    ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            # exchange + save in the same process, log everything to a file
            r = subprocess.run([sys.executable, "-u", "viewer_oauth.py", "--code", code],
                               cwd=os.path.dirname(os.path.abspath(__file__)),
                               capture_output=True, text=True, timeout=90)
            with open(os.path.join(BASE, "oauth_exchange.log"), "a") as f:
                f.write(f"=== {time.strftime('%H:%M:%S')} code_len={len(code)} head={code[:12]}...\n")
                f.write(f"stdout: {r.stdout}\nstderr: {r.stderr}\n\n")
            sys.stdout.write(f"[captured code len={len(code)}, head={code[:12]}...]\n")
            sys.stdout.write((r.stdout or "") + "\n" + (r.stderr or "") + "\n")
            sys.stdout.flush()
            global _DONE
            _DONE = True
        else:
            # any other path (e.g. http://localhost/) -> the auto-redirect page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(AUTH_PAGE)))
            self.end_headers()
            self.wfile.write(AUTH_PAGE)

    def log_message(self, format, *args):
        print("[listen]", format % args if args else format)


_DONE = False

if __name__ == "__main__":
    print(f"Waiting on http://localhost:{PORT}/  — open the OAuth URL in your browser now...")
    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
