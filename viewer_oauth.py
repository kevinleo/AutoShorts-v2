#!/usr/bin/env python3
"""viewer_oauth.py — OAuth for the PERSONAL viewing account (watch-history access).

Kept fully separate from the channel upload token (token.json) so the two accounts
never mix. Stores its own token in viewer_token.json with only the youtube.readonly
scope (no upload rights).

Usage:
  # 1) print the URL to open in a browser (logged in as the viewing account)
  python viewer_oauth.py --print-url
  # 2) after authorizing, the browser lands on  http://localhost/<AUTH_CODE>
  #    paste that code here (or use --code):
  python viewer_oauth.py --code 4/0AbC...
  # 3) verify
  python viewer_oauth.py --check

Env overrides (or set them in .env):
  VIEWER_CLIENT_FILE   (default: ./credentials.json)
  VIEWER_SECRETS_FILE  (default: ./client_secrets.json)
  VIEWER_TOKEN_FILE    (default: ./viewer_token.json)
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import env  # loads .env before the paths below are resolved

CLIENT_FILE = os.environ.get("VIEWER_CLIENT_FILE", os.path.join(BASE, "credentials.json"))
SECRETS_FILE = os.environ.get("VIEWER_SECRETS_FILE", os.path.join(BASE, "client_secrets.json"))
TOKEN_FILE = os.environ.get("VIEWER_TOKEN_FILE", os.path.join(BASE, "viewer_token.json"))

# Watch history only needs read-only. No upload scope on this account.
SCOPES = "https://www.googleapis.com/auth/youtube.readonly"
REDIRECT = "http://localhost"  # registered in credentials.json; code lands in the address bar


def _client():
    # client_secrets.json holds the "installed" client (http://localhost redirect)
    d = json.load(open(SECRETS_FILE))
    inst = d.get("installed", d)
    return inst["client_id"], inst["client_secret"]


def _web():
    d = json.load(open(CLIENT_FILE))
    return d.get("web", d)


def print_url():
    cid, csecret = _client()
    q = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    url = "https://accounts.google.com/o/oauth2/auth?" + q
    print("Open this in a browser (logged in as your VIEWING account):\n")
    print(url, "\n")
    print("After clicking 'Agree', the browser redirects to:")
    print("    http://localhost/4/0AbC...AUTH_CODE...")
    print("(a 'can't reach this page' error is EXPECTED — just copy the code from the bar)")
    print("Then paste it back:  python viewer_oauth.py --code <that code>")


def exchange(code: str):
    cid, csecret = _client()
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[FAIL] token exchange HTTP {e.code}: {body}")
        print("       (client_id:", cid[:24] + "..., redirect:", REDIRECT, ")")
        sys.exit(1)
    tok = {
        "access_token": r["access_token"],
        "refresh_token": r.get("refresh_token"),
        "token_type": r.get("token_type"),
        "expires_in": r.get("expires_in"),
        "scopes": SCOPES,
        "client_id": cid,
        "client_secret": csecret,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    print(f"[OK] viewer token saved to {TOKEN_FILE}")
    print("    refresh_token:", "yes" if tok.get("refresh_token") else "NO (re-run with prompt=consent if missing)")


def refresh():
    tok = json.load(open(TOKEN_FILE))
    data = urllib.parse.urlencode({
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    tok["access_token"] = r["access_token"]
    tok["token"] = r["access_token"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    return tok["access_token"]


def check():
    if not os.path.exists(TOKEN_FILE):
        print("no viewer token yet — run --print-url then --code")
        sys.exit(1)
    tok = json.load(open(TOKEN_FILE))
    try:
        access = tok["access_token"]
    except Exception:
        access = None
    # try current token, refresh if it fails
    import time
    def call(at):
        req = urllib.request.Request(
            "https://www.googleapis.com/youtube/v3/watchHistory?part=snippet&maxResults=1",
            headers={"Authorization": f"Bearer {at}"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    try:
        d = call(access)
    except Exception:
        print("token expired — refreshing...")
        access = refresh()
        d = call(access)
    n = d.get("pageInfo", {}).get("totalResults", 0)
    print(f"[OK] viewer account connected — watch history total = {n} items")


if __name__ == "__main__":
    if "--print-url" in sys.argv:
        print_url()
    elif "--check" in sys.argv:
        check()
    elif "--code" in sys.argv:
        i = sys.argv.index("--code")
        code = sys.argv[i + 1].strip()
        if code.startswith("http://localhost/"):
            code = code.rsplit("/", 1)[-1]
        exchange(code)
        check()
    else:
        print(__doc__)
