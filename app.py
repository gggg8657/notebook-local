#!/usr/bin/env python3
"""notebook local — NotebookLM 클론(경량). 문서를 올리면 출처 인용 채팅·노트·스튜디오(요약·FAQ·팟캐스트).
외부 파이썬 의존성 없음(stdlib). 문서 파싱은 같은 폴더 node_modules 의 kordoc(npm install 한 번).

  npm install && python3 app.py                       # http://localhost:8769
  LLM_API=openai LLM_BASE_URL=http://gpu:8000/v1 LLM_MODEL=Qwen3-32B python3 app.py
  EMBED_MODEL=bge-m3 TTS_BASE_URL=http://localhost:8771/v1 python3 app.py
  python3 app.py --cli add 노트북이름 문서.hwpx
  python3 app.py --cli ask 노트북이름 "예산은 얼마인가"

파이프라인:
  kordoc parse(문서→Markdown) → 청크(헤딩 breadcrumb, ~800자, 겹침 100) → 임베딩(EMBED_MODEL) 또는 BM25
  → 질문: 임베딩 코사인 + BM25 를 RRF 로 융합 → 상위 8개를 [n] 번호로 LLM 에 → 인용 달린 답변(스트리밍)
  → 스튜디오: 요약·FAQ·스터디가이드·타임라인·브리핑·팟캐스트 대본(→ TTS 서버 있으면 mp3)
"""
import base64
import datetime
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.environ.get("WORKSPACE") or os.path.join(ROOT, "_workspace")  # 포털이 AGENT_DATA/<도구> 로 모아 줌
DB = os.path.join(WS, "notebook.db")
LLM_API = os.environ.get("LLM_API", "ollama")            # ollama | openai (vLLM·LM Studio·llama.cpp 등)
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1" if LLM_API == "openai" else "http://localhost:11434").rstrip("/")
OLLAMA = LLM_BASE
MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
TTS_BASE = os.environ.get("TTS_BASE_URL", "").rstrip("/")   # OpenAI 호환 /v1/audio/speech (예: http://localhost:8771/v1)
TTS_MODEL = os.environ.get("TTS_MODEL", "kokoro")
TTS_VOICES = (os.environ.get("TTS_VOICE_A", "af_heart"), os.environ.get("TTS_VOICE_B", "am_adam"))
PORT = int(os.environ.get("PORT", "8769"))
NUM_CTX = int(os.environ.get("NUM_CTX", "16384"))
MAX_CHARS = int(os.environ.get("MAX_CHARS", "20000"))   # 스튜디오에 넣는 출처 본문 상한
TOP_K = int(os.environ.get("TOP_K", "8"))
CHUNK, OVERLAP = 800, 100
EXTS = (".hwp", ".hwpx", ".hml", ".pdf", ".docx", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp", ".md", ".txt")
STUDIO = ("summary", "faq", "guide", "timeline", "briefing", "podcast")
STUDIO_LABEL = {"summary": "전체 요약", "faq": "FAQ", "guide": "스터디 가이드", "timeline": "타임라인", "briefing": "브리핑 문서", "podcast": "팟캐스트 대본"}
_CLI = os.path.join(ROOT, "node_modules", "kordoc", "dist", "cli.js")
KORDOC = ["node", _CLI]  # npx 폴백 없음 — 폐쇄망에서 npx 는 무한 대기


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def kordoc(*args, cwd=None):
    r = subprocess.run([*KORDOC, *args], capture_output=True, text=True, cwd=cwd or ROOT, timeout=600)
    return r.returncode, (r.stdout + r.stderr).strip()


# ── LLM (kordoc-local 과 동일) ───────────────────────────────────────────
def _clean(out):
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    out = re.sub(r"^```\w*\s*\n", "", out)
    out = re.sub(r"\n?```\s*$", "", out)
    return out.strip()


def _hdr():
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})}


def openai_chat(system, user, model, on_token=None):
    body = {"model": model, "stream": True, "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(LLM_BASE + "/chat/completions", json.dumps(body).encode(), _hdr())
    buf = []
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                tok = (json.loads(line[5:])["choices"][0].get("delta") or {}).get("content") or ""
                if tok:
                    buf.append(tok)
                    if on_token:
                        on_token(tok)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    return "".join(buf)


def ollama(system, user, model, on_token=None):
    """Ollama /api/chat 스트리밍. think:false 미지원 모델이면 재시도. LLM_API=openai 면 OpenAI 호환으로."""
    if LLM_API == "openai":
        return openai_chat(system, user, model, on_token)
    body = {"model": model, "stream": True, "think": False,
            "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
            buf = []
            with urllib.request.urlopen(req, timeout=3600) as r:
                for line in r:
                    if not line.strip():
                        continue
                    j = json.loads(line)
                    if "error" in j:
                        raise RuntimeError(j["error"])
                    tok = j.get("message", {}).get("content", "")
                    if tok:
                        buf.append(tok)
                        if on_token:
                            on_token(tok)
                    if j.get("done"):
                        break
            return "".join(buf)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if attempt == 0 and "think" in msg:
                body.pop("think")
                continue
            raise RuntimeError(f"Ollama HTTP {e.code}: {msg[:300]}")


def models():
    if LLM_API == "openai":
        req = urllib.request.Request(LLM_BASE + "/models", headers=_hdr())
        with urllib.request.urlopen(req, timeout=10) as r:
            return [m["id"] for m in json.load(r)["data"]]
    with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]




def embed(texts):
    """임베딩 벡터 리스트. 실패하면 None → 호출자가 BM25 로 폴백."""
    if not texts:
        return None
    try:
        if LLM_API == "openai":
            req = urllib.request.Request(LLM_BASE + "/embeddings", json.dumps({"model": EMBED_MODEL, "input": texts}).encode(), _hdr())
            with urllib.request.urlopen(req, timeout=600) as r:
                return [d["embedding"] for d in json.load(r)["data"]]
        req = urllib.request.Request(OLLAMA + "/api/embed", json.dumps({"model": EMBED_MODEL, "input": texts}).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["embeddings"]
    except Exception as e:
        print(f"[embed] 실패 → BM25 폴백: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return None


def tts(text, voice):
    body = {"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "mp3"}
    req = urllib.request.Request(TTS_BASE + "/audio/speech", json.dumps(body).encode(), _hdr())
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


# ── 프롬프트 ────────────────────────────────────────────────────────────
def prompts():
    txt = read(os.path.join(ROOT, "goal-prompt.md"))
    common, *rest = re.split(r"^## (\w+)\s*$", txt, flags=re.M)
    return common.strip(), {rest[i]: rest[i + 1].strip() for i in range(0, len(rest), 2)}


# ── 저장소 ──────────────────────────────────────────────────────────────
def db():
    os.makedirs(WS, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS notebooks(id TEXT PRIMARY KEY, name TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, nb TEXT, name TEXT, kind TEXT, text TEXT, summary TEXT, included INTEGER DEFAULT 1, created TEXT);
    CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, src TEXT, nb TEXT, idx INTEGER, heading TEXT, text TEXT, emb TEXT);
    CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, nb TEXT, title TEXT, body TEXT, created TEXT);
    CREATE INDEX IF NOT EXISTS chunks_nb ON chunks(nb);
    """)
    return c


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def nid():
    return secrets.token_hex(4)


def nb_by_name(c, name):
    r = c.execute("SELECT * FROM notebooks WHERE id=? OR name=?", (name, name)).fetchone()
    return dict(r) if r else None


# ── 파싱·청크 ───────────────────────────────────────────────────────────
def parse(path, d):
    if path.lower().endswith((".md", ".txt")):
        return read(path)
    out = os.path.join(d, "source.md")
    code, msg = kordoc("--silent", path, "-o", out, cwd=d)
    if code != 0 or not os.path.exists(out):
        raise RuntimeError(f"kordoc 파싱 실패: {msg[-400:]}")
    return read(out)


def chunk_text(md, size=CHUNK, overlap=OVERLAP):
    """헤딩 breadcrumb 를 붙인 ~size 자 청크. 문단 단위로 쌓고 넘치면 자름, 앞 청크 꼬리 overlap 자를 앞에 붙임."""
    crumbs, out, buf, tail = [], [], [], ""

    def flush():
        nonlocal buf, tail
        body = "\n".join(buf).strip()
        if body:
            out.append((" > ".join(crumbs), (tail + "\n" + body).strip() if tail else body))
            tail = body[-overlap:] if overlap else ""
        buf = []

    for para in re.split(r"\n\s*\n", md):
        para = para.strip()
        if not para:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", para)
        if m:
            flush()
            lvl = len(m.group(1))
            crumbs[:] = crumbs[:lvl - 1] + [m.group(2).strip()]
            continue
        while len(para) > size:  # 긴 문단(표 등)은 강제 분할
            buf.append(para[:size]); flush(); para = para[size:]
        if sum(len(p) for p in buf) + len(para) > size:
            flush()
        buf.append(para)
    flush()
    return out


# ── 검색: BM25 + 코사인 → RRF ───────────────────────────────────────────
def tokens(s):
    """한글은 어절 + 2-gram(조사 변형 흡수), 영숫자는 소문자 단어."""
    out = []
    for w in re.findall(r"[가-힣]+|[a-zA-Z0-9]+", s):
        if re.match(r"[가-힣]", w):
            out.append(w)
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.append(w.lower())
    return out


def bm25(query, docs, k1=1.5, b=0.75):
    """docs: [(id, text)] → [(id, score)] 내림차순. ponytail: 질문마다 전체 재계산, 노트북당 수천 청크까진 충분."""
    q = tokens(query)
    toks = [tokens(t) for _, t in docs]
    n = len(docs) or 1
    avg = sum(len(t) for t in toks) / n
    df = Counter(w for t in toks for w in set(t))
    scores = []
    for (cid, _), t in zip(docs, toks):
        tf = Counter(t)
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / (avg or 1)))
        scores.append((cid, s))
    return sorted([x for x in scores if x[1] > 0], key=lambda x: -x[1])


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rrf(*ranked, k=60):
    s = Counter()
    for lst in ranked:
        for rank, (cid, _) in enumerate(lst):
            s[cid] += 1 / (k + rank + 1)
    return [cid for cid, _ in s.most_common()]


_VEC = {}  # ponytail: chunk id → 파싱된 벡터 캐시 (프로세스 수명), 수만 청크면 메모리 확인


def vec(r):
    v = _VEC.get(r["id"])
    if v is None:
        v = _VEC[r["id"]] = json.loads(r["emb"])
    return v


def retrieve(c, nb, question, top_k=TOP_K):
    rows = c.execute("SELECT ch.* , s.name AS sname FROM chunks ch JOIN sources s ON s.id=ch.src WHERE ch.nb=? AND s.included=1", (nb,)).fetchall()
    if not rows:
        return [], "none"
    lex = bm25(question, [(r["id"], r["heading"] + " " + r["text"]) for r in rows])
    qv = embed([question]) if any(r["emb"] for r in rows) else None
    mode = "bm25"
    if qv:
        sem = sorted([(r["id"], cosine(qv[0], vec(r))) for r in rows if r["emb"]], key=lambda x: -x[1])[:top_k * 3]
        order = rrf(lex[:top_k * 3], sem)
        mode = "hybrid"
    else:
        order = [cid for cid, _ in lex]
    by = {r["id"]: r for r in rows}
    return [dict(by[cid]) for cid in order[:top_k]], mode


# ── 파이프라인 ──────────────────────────────────────────────────────────
def add_source(nb, name, text=None, file=None, model=MODEL, emit=lambda ev: None):
    c = db()
    sid = nid()
    d = os.path.join(WS, nb, sid)
    os.makedirs(d, exist_ok=True)
    if file:
        if not file.lower().endswith(EXTS):
            raise ValueError(f"지원하지 않는 확장자: {os.path.basename(file)}")
        emit({"stage": "parse", "msg": f"kordoc 파싱 {name}"})
        text = parse(file, d)
        kind = os.path.splitext(name)[1].lstrip(".").lower()
    else:
        kind = "text"
    text = (text or "").strip()
    if not text:
        raise ValueError("빈 문서")
    write(os.path.join(d, "source.md"), text)
    chunks = chunk_text(text)
    emit({"stage": "embed", "msg": f"청크 {len(chunks)}개 임베딩 ({EMBED_MODEL})"})
    vecs = embed([h + "\n" + t for h, t in chunks])
    if vecs is None:
        emit({"stage": "embed", "msg": "임베딩 서버 없음 → BM25 전용"})
    emit({"stage": "summary", "msg": "출처 요약 (LLM)"})
    common, roles = prompts()
    summary = ""
    for attempt in (0, 1):  # ponytail: 모델 교체(임베딩→채팅) 직후 Ollama 러너가 죽는 경우가 있어 1회 재시도
        try:
            summary = _clean(ollama(common + "\n\n" + roles["source_summary"], text[:6000], model))
            break
        except Exception as e:
            summary = f"(요약 실패: {e})"
    c.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,1,?)", (sid, nb, name, kind, text, summary, now()))
    c.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                  [(f"{sid}-{i}", sid, nb, i, h, t, json.dumps(vecs[i]) if vecs else None) for i, (h, t) in enumerate(chunks)])
    c.commit()
    return {"id": sid, "name": name, "kind": kind, "chunks": len(chunks), "summary": summary, "embedded": bool(vecs)}


def ask(nb, question, history="", model=MODEL, emit=lambda ev: None):
    c = db()
    hits, mode = retrieve(c, nb, question)
    cites = [{"n": i + 1, "id": h["id"], "source": h["sname"], "heading": h["heading"], "text": h["text"]} for i, h in enumerate(hits)]
    emit({"cites": cites, "mode": mode})
    common, roles = prompts()
    ctx = "\n\n".join(f"[{x['n']}] ({x['source']}{' > ' + x['heading'] if x['heading'] else ''})\n{x['text']}" for x in cites) or "(출처 없음)"
    user = f"[출처]\n{ctx}\n\n" + (f"[이전 대화]\n{history}\n\n" if history else "") + f"[질문]\n{question}"
    answer = _clean(ollama(common + "\n\n" + roles["chat"], user, model, on_token=lambda t: emit({"token": t})))
    used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer) if 0 < int(n) <= len(cites)})
    return {"question": question, "answer": answer, "cites": cites, "used": used, "mode": mode, "model": model}


def source_text(c, nb, limit=MAX_CHARS):
    rows = c.execute("SELECT name, text FROM sources WHERE nb=? AND included=1 ORDER BY created", (nb,)).fetchall()
    if not rows:
        return "", False
    per = max(limit // len(rows), 500)
    parts = [f"### 출처: {r['name']}\n{r['text'][:per]}" for r in rows]
    return "\n\n".join(parts), any(len(r["text"]) > per for r in rows)


def studio(nb, kind, model=MODEL, emit=lambda ev: None):
    if kind not in STUDIO:
        raise ValueError(f"kind 는 {STUDIO} 중 하나")
    c = db()
    text, truncated = source_text(c, nb)
    if not text:
        raise ValueError("포함된 출처가 없습니다")
    common, roles = prompts()
    emit({"stage": "llm", "msg": f"{STUDIO_LABEL[kind]} 생성 ({model})", "reset": True})
    out = _clean(ollama(common + "\n\n" + roles[kind], text + ("\n\n(※ 출처가 길어 앞부분만 제공됨)" if truncated else ""), model,
                        on_token=lambda t: emit({"token": t})))
    title = f"{STUDIO_LABEL[kind]} · {datetime.date.today()}"
    note_id = nid()
    audio = None
    if kind == "podcast" and TTS_BASE:
        lines = [(m.group(1), m.group(2).strip()) for m in re.finditer(r"^\s*([AB])\s*[:：]\s*(.+)$", out, re.M)]
        d = os.path.join(WS, nb, "audio")
        os.makedirs(d, exist_ok=True)
        parts = []
        try:
            for i, (who, line) in enumerate(lines):
                emit({"stage": "tts", "msg": f"음성 합성 {i + 1}/{len(lines)}"})
                p = os.path.join(d, f"{note_id}-{i:03d}.mp3")
                with open(p, "wb") as f:
                    f.write(tts(line, TTS_VOICES[0] if who == "A" else TTS_VOICES[1]))
                parts.append(p)
            final = os.path.join(d, f"{note_id}.mp3")
            if shutil.which("ffmpeg"):
                lst = os.path.join(d, f"{note_id}.txt")
                write(lst, "".join(f"file '{p}'\n" for p in parts))
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final], check=True)
            else:  # ponytail: mp3 프레임 단순 이어붙이기 — 대부분 재생됨
                with open(final, "wb") as f:
                    for p in parts:
                        f.write(open(p, "rb").read())
            for p in parts:
                os.remove(p)
            audio = f"{note_id}.mp3"
        except Exception as e:
            emit({"stage": "tts", "msg": f"TTS 실패: {e}"})
    body = out + (f"\n\n<!-- audio: {audio} -->" if audio else "")
    c.execute("INSERT INTO notes VALUES(?,?,?,?,?)", (note_id, nb, title, body, now()))
    c.commit()
    return {"note_id": note_id, "title": title, "body": out, "audio": audio, "tts": bool(TTS_BASE), "truncated": truncated}


# ── HTTP ───────────────────────────────────────────────────────────────
HTML = read(os.path.join(ROOT, "ui.html")) if os.path.exists(os.path.join(ROOT, "ui.html")) else "ui.html 없음"


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "POST" in (a[0] if a else ""):
            super().log_message(fmt, *a)

    def _send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        return emit

    def do_GET(self):
        try:
            c = db()
            if self.path == "/api/models":
                return self._send({"models": models(), "default": MODEL, "embed": EMBED_MODEL, "tts": bool(TTS_BASE)})
            if self.path == "/api/notebooks":
                return self._send([dict(r) for r in c.execute("SELECT * FROM notebooks ORDER BY created DESC")])
            m = re.fullmatch(r"/api/nb/(\w+)", self.path)
            if m:
                nb = m.group(1)
                return self._send({
                    "notebook": nb_by_name(c, nb),
                    "sources": [dict(r) for r in c.execute("SELECT id,name,kind,summary,included,created,length(text) AS chars,(SELECT count(*) FROM chunks WHERE src=sources.id) AS chunks,(SELECT count(*) FROM chunks WHERE src=sources.id AND emb IS NOT NULL) AS embedded FROM sources WHERE nb=? ORDER BY created", (nb,))],
                    "notes": [dict(r) for r in c.execute("SELECT * FROM notes WHERE nb=? ORDER BY created DESC", (nb,))]})
            m = re.fullmatch(r"/api/nb/(\w+)/audio/([\w-]+\.mp3)", self.path)
            if m:
                with open(os.path.join(WS, m.group(1), "audio", m.group(2)), "rb") as f:
                    return self._send(f.read(), "audio/mpeg")
            self._send(HTML.encode(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send({"error": "없음"}, code=404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        c = db()
        try:
            if self.path == "/api/notebooks":
                op = req.get("op", "create")
                if op == "create":
                    i = nid()
                    c.execute("INSERT INTO notebooks VALUES(?,?,?)", (i, (req.get("name") or "새 노트북").strip(), now()))
                elif op == "rename":
                    c.execute("UPDATE notebooks SET name=? WHERE id=?", (req["name"].strip(), req["id"]))
                elif op == "delete":
                    for t in ("notebooks", "sources", "chunks", "notes"):
                        c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'notebooks' else 'nb'}=?", (req["id"],))
                    shutil.rmtree(os.path.join(WS, req["id"]), ignore_errors=True)
                c.commit()
                return self._send({"ok": True})
            m = re.fullmatch(r"/api/nb/(\w+)/(source|chat|note|studio)", self.path)
            if not m:
                return self._send({"error": "없는 경로"}, code=404)
            nb, what = m.groups()
            if what == "source":
                op = req.get("op", "add")
                if op == "toggle":
                    c.execute("UPDATE sources SET included=1-included WHERE id=?", (req["id"],)); c.commit()
                    return self._send({"ok": True})
                if op == "delete":
                    c.execute("DELETE FROM chunks WHERE src=?", (req["id"],)); c.execute("DELETE FROM sources WHERE id=?", (req["id"],)); c.commit()
                    shutil.rmtree(os.path.join(WS, nb, req["id"]), ignore_errors=True)
                    return self._send({"ok": True})
                emit = self._sse()
                file = None
                if req.get("file_b64"):
                    name = re.sub(r"[^\w.\-가-힣 ]", "_", os.path.basename(req.get("file_name") or "upload.bin"))
                    os.makedirs(os.path.join(WS, "_upload"), exist_ok=True)
                    file = os.path.join(WS, "_upload", name)
                    with open(file, "wb") as f:
                        f.write(base64.b64decode(req["file_b64"]))
                try:
                    emit({"done": add_source(nb, req.get("file_name") or req.get("name") or "붙여넣은 글", req.get("text"), file, req.get("model") or MODEL, emit)})
                except Exception as e:
                    emit({"error": f"{type(e).__name__}: {e}"})
                finally:
                    if file:
                        os.remove(file)
                return
            if what == "chat":
                emit = self._sse()
                try:
                    emit({"done": ask(nb, (req.get("question") or "").strip(), req.get("history") or "", req.get("model") or MODEL, emit)})
                except Exception as e:
                    emit({"error": f"{type(e).__name__}: {e}"})
                return
            if what == "note":
                op = req.get("op", "save")
                if op == "save":
                    i = req.get("id") or nid()
                    c.execute("INSERT OR REPLACE INTO notes VALUES(?,?,?,?,?)", (i, nb, (req.get("title") or "노트").strip(), req.get("body") or "", now()))
                elif op == "delete":
                    c.execute("DELETE FROM notes WHERE id=?", (req["id"],))
                elif op == "to_source":
                    n = c.execute("SELECT * FROM notes WHERE id=?", (req["id"],)).fetchone()
                    c.commit()
                    return self._send(add_source(nb, "노트: " + n["title"], n["body"], None, req.get("model") or MODEL))
                c.commit()
                return self._send({"ok": True})
            if what == "studio":
                emit = self._sse()
                try:
                    emit({"done": studio(nb, req.get("kind"), req.get("model") or MODEL, emit)})
                except Exception as e:
                    emit({"error": f"{type(e).__name__}: {e}"})
                return
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)


def _cli_nb(name):
    c = db()
    r = nb_by_name(c, name)
    if not r:
        i = nid()
        c.execute("INSERT INTO notebooks VALUES(?,?,?)", (i, name, now())); c.commit()
        r = {"id": i}
    return r["id"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        log = lambda ev: print(f"[{ev['stage']}] {ev['msg']}", file=sys.stderr) if "stage" in ev else None
        op, nb = sys.argv[2], _cli_nb(sys.argv[3])
        if op == "add":
            print(json.dumps(add_source(nb, os.path.basename(sys.argv[4]), None, sys.argv[4], MODEL, log), ensure_ascii=False, indent=1))
        elif op == "ask":
            r = ask(nb, sys.argv[4], "", MODEL, log)
            print(r["answer"]); print("\n--- 출처 ---")
            for x in r["cites"]:
                print(f"[{x['n']}] {x['source']} > {x['heading']}: {x['text'][:80]!r}")
        elif op == "studio":
            print(studio(nb, sys.argv[4], MODEL, log)["body"])
        sys.exit(0)
    if not os.path.exists(_CLI):
        sys.exit("node_modules/kordoc 없음 — 이 폴더에서 `npm install` 또는 pack.sh 번들을 쓰세요")
    print(f"notebook local → http://localhost:{PORT}  (llm={LLM_API} {LLM_BASE} model={MODEL} embed={EMBED_MODEL} tts={TTS_BASE or '없음'})")
    ThreadingHTTPServer(("", PORT), H).serve_forever()
