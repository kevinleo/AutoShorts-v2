#!/usr/bin/env python3
"""knowledge_base.py — hybrid RAG + LLM-wiki knowledge store for the pipeline.

Replaces "dump everything into one memory file". Structure:
  kb/kb.db          SQLite: docs + FTS5 (BM25) + embeddings (numpy float32 BLOBs)
  kb/wiki/*.md      LLM-wiki: one markdown doc per durable topic
                    (taste-profile, workflow-insights, topic-recipes, ...)
                    synced into the DB so wiki pages are searchable too.

Search = hybrid:  0.5 * normalized BM25  +  0.5 * cosine(embedding)
Embeddings: local llama.cpp (nomic-embed-text-v1.5 Q8) serving /v1/embeddings — $0.
Set EMBED_URL in .env to point at it (default: http://127.0.0.1:8086/v1/embeddings).
If the embedder (or numpy) is unavailable, search degrades gracefully to BM25-only.

Kinds:
  taste    — viewer taste signals (top categories, channels, formats)
  insight  — research findings useful for the workflow itself
  topic    — topic recipes (what worked: scores, hooks, formats)
  note     — anything else durable

Usage:
  python knowledge_base.py sync                       # index kb/wiki/*.md into DB
  python knowledge_base.py add --kind insight --title "..." --content "..."
  python knowledge_base.py search "hook that stops scroll" 5
  python knowledge_base.py stats
"""
import argparse, json, math, os, re, sqlite3, sys, time, urllib.request

import env  # loads .env (EMBED_URL) before the defaults below

BASE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(BASE, "kb")
DB = os.path.join(KB_DIR, "kb.db")
WIKI = os.path.join(KB_DIR, "wiki")
EMBED_URL = os.getenv("EMBED_URL", "http://127.0.0.1:8086/v1/embeddings")
EMBED_MODEL = "nomic"
DIM = 768  # nomic-embed-text-v1.5

con = sqlite3.connect(DB)
con.execute("""CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY,
    kind TEXT, title TEXT, content TEXT,
    source TEXT, tags TEXT,
    created REAL, file TEXT, mtime REAL,
    para TEXT,
    UNIQUE(kind, title)
)""")
# migrate: add para column if this DB predates it
_cols = [r[1] for r in con.execute("PRAGMA table_info(docs)")]
if "para" not in _cols:
    con.execute("ALTER TABLE docs ADD COLUMN para TEXT")
con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, content, content='docs', content_rowid='id')""")
con.execute("""CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content); END""")
con.execute("""CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content); END""")
con.execute("""CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
    INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content); END""")
con.execute("CREATE TABLE IF NOT EXISTS emb(id INTEGER PRIMARY KEY, vec BLOB)")
con.commit()
os.makedirs(WIKI, exist_ok=True)


# ---------- embeddings ----------
def _embed_batch(texts, retries=2):
    """Return list[np.ndarray float32] or None if embedder unavailable."""
    global _NP
    try:
        import numpy as np
        _NP = np
    except Exception:
        return None
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(EMBED_URL, data=payload,
                                         headers={"Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            vecs = []
            for item in d["data"]:
                v = _NP.asarray(item["embedding"], dtype="float32")
                n = _NP.linalg.norm(v)
                if n > 0:
                    v = v / n
                vecs.append(v)
            return vecs
        except Exception:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return None


def _embed(text):
    out = _embed_batch([text])
    return out[0] if out else None


def _store_emb(doc_id, text):
    v = _embed(text)
    if v is None:
        return False
    con.execute("INSERT OR REPLACE INTO emb(id, vec) VALUES(?, ?)",
                (doc_id, v.tobytes()))
    con.commit()
    return True


# ---------- PARA (second brain) ----------
# para derived from the wiki folder the doc lives in:
#   00-projects/  -> projects   (active work: the pipeline itself, campaigns)
#   10-areas/     -> areas      (ongoing: taste, workflow-quality)
#   20-resources/ -> resources  (evergreen reference: techniques, facts, tools)
#   30-archives/  -> archives   (finished/dated: old recipes, dead leads)
#   (root wiki)   -> areas
FOLDER_PARA = {"00-projects": "projects", "10-areas": "areas",
               "20-resources": "resources", "30-archives": "archives"}


def infer_para(file=None, kind=None, text=""):
    """Para bucket: explicit folder > explicit <!-- para: --> tag > kind default."""
    if file:
        top = file.replace("\\", "/").split("/")[0]
        if top in FOLDER_PARA:
            return FOLDER_PARA[top]
    m = re.search(r"<!--\s*para:\s*(\w+)\s*-->", text or "")
    if m and m.group(1) in ("projects", "areas", "resources", "archives"):
        return m.group(1)
    return {"taste": "areas", "insight": "areas", "topic": "resources",
            "note": "areas"}.get(kind or "note", "areas")


# ---------- add ----------
def add(kind, title, content, source="", tags="", para=None):
    content = content.strip()
    para = para or infer_para(kind=kind, text=content)
    con.execute(
        "INSERT OR REPLACE INTO docs(kind,title,content,source,tags,created,para) "
        "VALUES(?,?,?,?,?,?,?)",
        (kind, title.strip(), content, source, tags, time.time(), para))
    doc_id = con.execute(
        "SELECT id FROM docs WHERE kind=? AND title=?", (kind, title.strip())).fetchone()[0]
    con.commit()
    _store_emb(doc_id, title + "\n" + content)
    return doc_id


def upsert_file(kind, title, content, source, tags, file, mtime, para=None):
    """For wiki files: skip re-embedding if unchanged."""
    row = con.execute("SELECT id, mtime, para FROM docs WHERE file=? AND kind=?",
                       (file, kind)).fetchone()
    if row and row[1] == mtime and row[2]:
        return None
    para = para or infer_para(file=file, kind=kind, text=content)
    con.execute(
        "INSERT OR REPLACE INTO docs(kind,title,content,source,tags,created,file,mtime,para) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, title.strip(), content, source, tags, time.time(), file, mtime, para))
    doc_id = con.execute(
        "SELECT id FROM docs WHERE kind=? AND title=?", (kind, title.strip())).fetchone()[0]
    con.commit()
    _store_emb(doc_id, title + "\n" + content)
    return doc_id


# ---------- wiki sync (recursive, PARA folders) ----------
def sync_wiki():
    n = 0
    for root, _dirs, files in os.walk(WIKI):
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, WIKI).replace(os.sep, "/")
            text = open(path, encoding="utf-8").read()
            first = next((l.strip("# ").strip() for l in text.splitlines() if l.startswith("# ")), fn)
            mtime = os.path.getmtime(path)
            m = re.search(r"<!--\s*kind:\s*(\w+)\s*-->", text)
            kind = m.group(1) if m else "note"
            src = re.search(r"<!--\s*source:\s*(.+?)\s*-->", text)
            tags = re.search(r"<!--\s*tags:\s*(.+?)\s*-->", text)
            if upsert_file(kind, first, text, src.group(1) if src else "wiki",
                           tags.group(1) if tags else "", rel, mtime) is not None:
                n += 1
    # prune docs whose wiki file no longer exists (moved/renamed/deleted);
    # the docs_ad trigger handles the FTS side automatically
    for doc_id, fname in con.execute("SELECT id, file FROM docs WHERE file IS NOT NULL").fetchall():
        if not os.path.exists(os.path.join(WIKI, fname)):
            con.execute("DELETE FROM emb WHERE id=?", (doc_id,))
            con.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            n += 1
    # backfill para for docs created before the column existed
    for doc_id, kind, fname, content in con.execute(
            "SELECT id, kind, file, content FROM docs WHERE para IS NULL").fetchall():
        con.execute("UPDATE docs SET para=? WHERE id=?",
                    (infer_para(file=fname, kind=kind, text=content), doc_id))
        n += 1
    con.commit()
    return n


# ---------- search ----------
_bootstrapped = False


def _ensure_indexed():
    """A fresh clone ships an empty kb.db (git-ignored), so index the wiki on the
    first search: day-1 topic picks get KB context instead of "(no indexed recipes
    yet)". Cheap (a handful of small files) and never fatal."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    try:
        if con.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0:
            n = sync_wiki()
            if n:
                print(f"[KB] indexed {n} wiki docs", file=sys.stderr)
    except Exception as e:  # indexing must never break a search
        print(f"[KB] index bootstrap skipped: {e}", file=sys.stderr)


def _bm25(query, k):
    try:
        # OR-join: an AND query over a whole sentence almost never matches, which
        # silently returned zero hits for the topic-pick prompt in daily_generate.sh.
        q = " OR ".join(f'"{w}"' for w in re.findall(r"[\w-]{2,}", query)[:12])
        if not q:
            return []
        rows = con.execute(
            "SELECT rowid, bm25(docs_fts) AS s FROM docs_fts WHERE docs_fts MATCH ? "
            "ORDER BY s LIMIT ?", (q, k * 4)).fetchall()
        return [(r[0], -r[1]) for r in rows]  # higher = better
    except Exception:
        return []


def _vec_search(query, k):
    qv = _embed(query)
    if qv is None:
        return []
    import numpy as np
    out = []
    for doc_id, blob in con.execute("SELECT id, vec FROM emb").fetchall():
        v = np.frombuffer(blob, dtype="float32")
        if v.shape != qv.shape:
            continue
        out.append((doc_id, float(np.dot(qv, v))))
    out.sort(key=lambda x: -x[1])
    return out[:k * 4]


def search(query, k=5, kind=None):
    _ensure_indexed()
    b = dict(_bm25(query, k))
    v = dict(_vec_search(query, k))
    ids = set(b) | set(v)
    if not ids:
        return []
    # normalize each list to [0,1]
    def norm(d):
        if not d:
            return {}
        vals = list(d.values())
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return {i: 0.5 for i in d}
        return {i: (x - lo) / (hi - lo) for i, x in d.items()}
    bn, vn = norm(b), norm(v)
    scored = []
    for i in ids:
        s = 0.0
        if i in bn or i in vn:
            s = 0.5 * bn.get(i, 0.0) + 0.5 * vn.get(i, 0.0)
        scored.append((i, s))
    scored.sort(key=lambda x: -x[1])
    rows = []
    for i, s in scored[:k]:
        row = con.execute(
            "SELECT id, kind, title, content, source, tags, created, para FROM docs WHERE id=?",
            (i,)).fetchone()
        if row and (kind is None or row[1] == kind):
            rows.append({
                "id": row[0], "kind": row[1], "title": row[2],
                "content": row[3], "source": row[4], "tags": row[5],
                "para": row[7],
                "score": round(s, 3),
            })
    return rows


def stats():
    d = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    e = con.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
    kinds = con.execute("SELECT kind, COUNT(*) FROM docs GROUP BY kind").fetchall()
    paras = con.execute("SELECT COALESCE(para,'?'), COUNT(*) FROM docs GROUP BY para").fetchall()
    n_wiki = 0
    for _root, _dirs, files in os.walk(WIKI):
        n_wiki += len([f for f in files if f.endswith(".md")])
    return {"docs": d, "embedded": e, "kinds": dict(kinds),
            "para": dict(paras), "wiki_files": n_wiki}


def tree():
    """Second-brain overview: PARA folders + doc counts."""
    lines = ["Second Brain (kb/wiki) — PARA structure", ""]
    for top in ("00-projects", "10-areas", "20-resources", "30-archives"):
        p = os.path.join(WIKI, top)
        if not os.path.isdir(p):
            continue
        files = []
        for root, _dirs, fs in os.walk(p):
            files += [os.path.relpath(os.path.join(root, f), WIKI) for f in sorted(fs)
                      if f.endswith(".md")]
        lines.append(f"{top}/  ({len(files)} docs)")
        for f in files:
            lines.append(f"  - {f}")
    root_files = [f for f in sorted(os.listdir(WIKI))
                  if os.path.isfile(os.path.join(WIKI, f)) and f.endswith(".md")]
    if root_files:
        lines.append(f"(wiki root)  ({len(root_files)} docs, treated as areas)")
        lines += [f"  - {f}" for f in root_files]
    return "\n".join(lines)


def main(argv):
    if argv[0] == "sync":
        print("synced", sync_wiki(), "wiki files")
    elif argv[0] == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif argv[0] == "tree":
        print(tree())
    elif argv[0] == "search" and len(argv) >= 3:
        k = int(argv[-1]) if argv[-1].isdigit() else 5
        q = " ".join(argv[2:-1]) if argv[-1].isdigit() else " ".join(argv[2:])
        for r in search(q, k=k):
            print(f"[{r['score']:.2f}] ({r['para']}/{r['kind']}) {r['title']}  [src={r['source']}]")
            print("   ", r["content"][:220].replace("\n", " "))
    elif argv[0] == "add" and len(argv) >= 5:
        kind, title, content = argv[1], argv[2], " ".join(argv[4:])
        i = add(kind, title, content)
        print("added doc", i)
    else:
        print("usage: knowledge_base.py <sync|stats|tree|search <query> [k]|add <kind> <title> <content>>")


if __name__ == "__main__":
    import sys
    main(sys.argv[1:] or ["stats"])
