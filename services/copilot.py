# services/copilot.py
import os
import re
import json
import time
import glob
import pathlib
import hashlib
import numpy as np
import markdown
import requests
from typing import List, Dict, Tuple

OLLAMA = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.getenv("COPILOT_EMBED_MODEL", "nomic-embed-text")
LLM_MODEL = os.getenv("COPILOT_LLM", "llama3.2")
DOCS_DIR = os.getenv("COPILOT_DOCS_DIR", "./docs")
INDEX_DIR = os.getenv("COPILOT_INDEX_DIR", "./instance/copilot_index")
TOP_K = int(os.getenv("COPILOT_TOP_K", "6"))
MIN_SIM = float(os.getenv("COPILOT_MIN_SIM", "0.28"))
RATE_LIMIT_PER_MIN = int(os.getenv("COPILOT_RATE_LIMIT_PER_MIN", "12"))

os.makedirs(INDEX_DIR, exist_ok=True)
VEC_PATH = os.path.join(INDEX_DIR, "vectors.npy")
META_PATH = os.path.join(INDEX_DIR, "meta.json")
SIG_PATH = os.path.join(INDEX_DIR, "signature.txt")


def _md_to_text(md: str) -> str:
    # Strip code blocks & mermaid to avoid polluting retrieval
    md = re.sub(r"```.*?```", "", md, flags=re.S)
    md = re.sub(r"```mermaid.*?```", "", md, flags=re.S | re.I)
    # Remove HTML comments
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    # Keep headings and paragraphs
    return md


def _chunk(md_text: str, file_path: str) -> List[Dict]:
    # simple header-aware chunking: split on H2/H3, keep small sections together
    chunks = []
    current = {"title": pathlib.Path(file_path).name, "anchor": "", "text": ""}
    for line in md_text.splitlines():
        if line.startswith("## "):     # H2
            if current["text"].strip():
                chunks.append(current)
            current = {"title": line[3:].strip(), "anchor": line[3:].strip(
            ).lower().replace(" ", "-"), "text": ""}
        elif line.startswith("### "):   # H3
            if current["text"].strip():
                chunks.append(current)
            current = {"title": line[4:].strip(), "anchor": line[4:].strip(
            ).lower().replace(" ", "-"), "text": ""}
        else:
            current["text"] += line + "\n"
    if current["text"].strip():
        chunks.append(current)

    # tighten chunk sizes (hard wrap around ~1200 chars)
    out = []
    for c in chunks:
        txt = re.sub(r"\s+\n", "\n", c["text"]).strip()
        while len(txt) > 1200:
            cut = txt[:1200]
            last_dot = cut.rfind(".")
            if last_dot < 600:
                last_dot = 1200
            out.append({**c, "text": txt[:last_dot].strip()})
            txt = txt[last_dot:].strip()
        if txt:
            out.append({**c, "text": txt})
    return out


def _embed(texts: List[str]) -> np.ndarray:
    # Call Ollama embeddings endpoint (one-by-one for simplicity)
    vecs = []
    for t in texts:
        r = requests.post(f"{OLLAMA}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": t}, timeout=60)
        r.raise_for_status()
        v = np.array(r.json()["embedding"], dtype=np.float32)
        # normalize for cosine via dot product
        n = np.linalg.norm(v) + 1e-9
        vecs.append(v / n)
    return np.vstack(vecs) if vecs else np.zeros((0, 384), dtype=np.float32)


def _signature() -> str:
    # hash of all files (mtime+size) so we only rebuild when docs change
    parts = []
    for fp in sorted(glob.glob(os.path.join(DOCS_DIR, "**", "*.md"), recursive=True)):
        st = os.stat(fp)
        parts.append(f"{fp}|{int(st.st_mtime)}|{st.st_size}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def build_index(force=False) -> Tuple[np.ndarray, List[Dict]]:
    sig = _signature()
    if (not force) and os.path.exists(VEC_PATH) and os.path.exists(META_PATH) and os.path.exists(SIG_PATH):
        if pathlib.Path(SIG_PATH).read_text().strip() == sig:
            vecs = np.load(VEC_PATH)
            meta = json.loads(pathlib.Path(META_PATH).read_text())
            return vecs, meta

    docs = []
    for fp in sorted(glob.glob(os.path.join(DOCS_DIR, "**", "*.md"), recursive=True)):
        md = pathlib.Path(fp).read_text(encoding="utf-8", errors="ignore")
        text = _md_to_text(md)
        for c in _chunk(text, fp):
            docs.append({
                "file": os.path.relpath(fp, DOCS_DIR).replace("\\", "/"),
                "title": c["title"] or pathlib.Path(fp).stem,
                "anchor": c["anchor"],
                "text": c["text"].strip()
            })
    if not docs:
        vecs = np.zeros((0, 384), dtype=np.float32)
        meta = []
    else:
        vecs = _embed([d["text"] for d in docs])
        meta = docs

    np.save(VEC_PATH, vecs)
    pathlib.Path(META_PATH).write_text(json.dumps(meta, ensure_ascii=False))
    pathlib.Path(SIG_PATH).write_text(sig)
    return vecs, meta


# global (lazy) cache
_VEC = None
_META = None
_LAST_LOAD = 0


def _ensure_index():
    global _VEC, _META, _LAST_LOAD
    if _VEC is None or (time.time() - _LAST_LOAD) > 60:
        _VEC, _META = build_index(force=False)
        _LAST_LOAD = time.time()


def _search(query: str, k: int = TOP_K) -> List[Tuple[float, Dict]]:
    _ensure_index()
    if _VEC is None or len(_META) == 0:
        return []
    qv = _embed([query])[0]
    sims = _VEC @ qv  # cosine (because both are normalized)
    idx = np.argsort(-sims)[:k]
    return [(float(sims[i]), _META[i]) for i in idx]


def _system_prompt() -> str:
    return (
        "You are the in-app assistant for the HPC Billing webapp.\n"
        "Answer briefly and only using the provided CONTEXT.\n"
        "If the context is insufficient or unrelated, say: I don't know.\n"
        "Always show sources as a bullet list at the end using the provided file/anchor.\n"
    )


def _format_context(hits: List[Tuple[float, Dict]]) -> Tuple[str, List[Dict]]:
    parts = []
    srcs = []
    for score, m in hits:
        parts.append(f"[{m['file']}#{m['anchor']}]\n{m['text']}\n")
        srcs.append({"file": m["file"], "anchor": m["anchor"],
                    "title": m["title"], "score": round(score, 3)})
    return "\n---\n".join(parts), srcs


def _ollama_chat(messages: List[Dict]) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", json={"model": LLM_MODEL,
                      "messages": messages, "stream": False}, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]


# naive per-IP leaky bucket (per-process)
_BUCKETS: Dict[str, List[float]] = {}


def _rate_limit(ip: str) -> bool:
    now = time.time()
    window = 60.0
    keep = []
    for t in _BUCKETS.get(ip, []):
        if now - t <= window:
            keep.append(t)
    if len(keep) >= RATE_LIMIT_PER_MIN:
        _BUCKETS[ip] = keep
        return False
    keep.append(now)
    _BUCKETS[ip] = keep
    return True


def ask(ip: str, question: str) -> Dict:
    if not _rate_limit(ip):
        return {"answer_html": "Rate limit exceeded. Please try again in a minute.", "sources": [], "from": "copilot"}

    hits = _search(question, TOP_K)
    if not hits or (hits[0][0] < MIN_SIM):
        return {"answer_html": "I don't know.", "sources": [], "from": "copilot"}

    ctx, sources = _format_context(hits)
    sys = _system_prompt()
    user = f"QUESTION:\n{question}\n\nCONTEXT:\n{ctx}\n\nAnswer now. If unsure, say 'I don't know.' Include sources."
    reply = _ollama_chat([
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ])
    return {"answer_html": reply, "sources": sources, "from": "copilot"}

# expose a quick rebuild for admin/ops


def rebuild():
    build_index(force=True)
