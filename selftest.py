#!/usr/bin/env python3
"""LLM·임베딩 서버 없이 검증: 청크 → BM25/하이브리드 검색 → 인용 매핑 → 노트 → kordoc 파싱(실제).  python3 selftest.py"""
import hashlib, os, shutil, tempfile
import app

app.WS = tempfile.mkdtemp(); app.DB = os.path.join(app.WS, "t.db")

def fake_embed(texts):  # 결정적 해시 벡터 (글자 2-gram 해시 → 64차원)
    out = []
    for t in texts:
        v = [0.0] * 64
        for g in app.tokens(t):
            v[int(hashlib.md5(g.encode()).hexdigest(), 16) % 64] += 1
        out.append(v)
    return out
calls = []
def fake_llm(system, user, model, on_token=None):
    calls.append(user); out = FAKE
    if on_token: on_token(out)
    return out
app.embed, app.ollama = fake_embed, fake_llm

# 1) 청크: 헤딩 breadcrumb + 겹침
md = "# 사업 개요\n\n" + "예산은 2026년 기준 12억 원이다. " * 10 + "\n\n## 추진 일정\n\n착수는 2026. 3. 1. 이고 종료는 2027. 2. 28. 이다.\n\n## 담당\n\n담당자는 홍길동 선임연구원이다."
ch = app.chunk_text(md, size=200, overlap=30)
assert ch[0][0] == "사업 개요" and ch[-1][0] == "사업 개요 > 담당", [h for h, _ in ch]
assert any("홍길동" in t for _, t in ch) and len(ch) >= 4, len(ch)

# 2) BM25: 한국어 조사 변형에도 맞는 청크가 1위
docs = [("a", "예산은 12억 원이다"), ("b", "담당자는 홍길동이다"), ("c", "착수 일정은 3월이다")]
assert app.bm25("홍길동 담당자", docs)[0][0] == "b" and app.bm25("예산이 얼마", docs)[0][0] == "a"

# 3) 노트북 + 텍스트 출처 + 하이브리드 검색 + 인용
FAKE = "요약 1줄\n요약 2줄\n요약 3줄"
nb = app._cli_nb("테스트")
s = app.add_source(nb, "개요.md", md)
assert s["chunks"] >= 2 and s["embedded"] and "요약 1줄" in s["summary"]
FAKE = "담당자는 홍길동 선임연구원이다 [2]. 예산은 12억 원이다 [1]."
r = app.ask(nb, "담당자가 누구야")
assert r["mode"] == "hybrid" and r["cites"] and any("홍길동" in c["text"] for c in r["cites"][:2]), [c["heading"] for c in r["cites"]]
assert r["used"] == [1, 2] and "[출처]" in calls[-1] and "[1]" in calls[-1]

# 4) 임베딩 실패 → BM25 폴백으로 계속 동작
app.embed = lambda texts: None
s2 = app.add_source(nb, "메모", "회의 장소는 대전 본원 3층 회의실이다.")
assert not s2["embedded"]
r = app.ask(nb, "회의 장소")
assert r["mode"] == "bm25" and "대전" in r["cites"][0]["text"]

# 5) 스튜디오 → 노트 저장, 팟캐스트는 TTS 미연결이면 대본만
FAKE = "A: 안녕하세요\nB: 반갑습니다"
app.TTS_BASE = ""
st = app.studio(nb, "podcast")
c = app.db(); assert c.execute("SELECT count(*) FROM notes WHERE nb=?", (nb,)).fetchone()[0] == 1 and st["audio"] is None and not st["tts"]

# 6) kordoc 실제 파싱 (node_modules 있을 때만)
sample = os.path.join(os.path.dirname(app.ROOT), "kordoc-local", "sample", "dummy.hwpx")
if os.path.exists(app._CLI) and os.path.exists(sample):
    FAKE = "요약"
    s3 = app.add_source(nb, "dummy.hwpx", None, sample)
    assert s3["chunks"] >= 1 and "자문" in app.db().execute("SELECT text FROM sources WHERE id=?", (s3["id"],)).fetchone()[0]
    print("kordoc 파싱 OK")
else:
    print("kordoc 생략 (node_modules 또는 샘플 없음)")

# 7) 브라우저가 떠나도(첫 이벤트 뒤 쓰기가 BrokenPipeError) 출처 추가·스튜디오는 끝까지 — 노트북에 남고 서버 오류·트레이스백 없음
import json
import io, sys, tempfile, threading, urllib.request
from http.server import ThreadingHTTPServer
class Cut:
    def __init__(s, f, n): s.f, s.n = f, n
    def write(s, b):
        s.n -= 1
        if s.n < 0: raise BrokenPipeError(32, "Broken pipe")
        return s.f.write(b)
    def flush(s): pass
    def __getattr__(s, k): return getattr(s.f, k)
class CutH(app.H):
    def setup(s): super().setup(); s.wfile = Cut(s.wfile, 2)  # 응답 머리 + 첫 이벤트까지만 닿음
ERRS = []
class Srv(ThreadingHTTPServer):
    def handle_error(s, req, addr): ERRS.append(sys.exc_info()[1])
def cut_post(path, body, user="alice"):
    srv = Srv(("127.0.0.1", 0), CutH); threading.Thread(target=srv.serve_forever, daemon=True).start()
    err0, sys.stderr = sys.stderr, io.StringIO()
    try:
        r = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", json.dumps(body).encode(), {"Content-Type": "application/json", "X-Agent-User": user})
        got = urllib.request.urlopen(r).read().decode()
    finally:
        cap, sys.stderr = sys.stderr.getvalue(), err0
    srv.shutdown(); srv.server_close()
    assert got.count("data: ") == 1 and not ERRS and "Traceback" not in cap and cap.count("연결 끊김") == 1, (got, ERRS, cap)
    return got
def fake2(system, user, model, on_token=None):
    for part in ("A: 떠난 ", "뒤\nB: 대본"):
        on_token and on_token(part)
    return "A: 떠난 뒤\nB: 대본"
app.ollama, app.embed = fake2, fake_embed
AW = os.path.join(app.WS, "u", "alice"); os.makedirs(AW, exist_ok=True)
anb = app.nid(); ac = app.db(AW); ac.execute("INSERT INTO notebooks VALUES(?,?,?)", (anb, "떠남", app.now())); ac.commit()
cut_post(f"/api/nb/{anb}/source", {"name": "떠난 뒤 출처", "text": md})
cut_post(f"/api/nb/{anb}/studio", {"kind": "podcast"})
ac = app.db(AW)
assert ac.execute("SELECT count(*) FROM sources WHERE nb=? AND name=?", (anb, "떠난 뒤 출처")).fetchone()[0] == 1
assert ac.execute("SELECT count(*) FROM chunks WHERE nb=?", (anb,)).fetchone()[0] >= 2
assert [r[0] for r in ac.execute("SELECT body FROM notes WHERE nb=?", (anb,))] == ["A: 떠난 뒤\nB: 대본"]

shutil.rmtree(app.WS)
# 저작권 표기: 서버가 화면에 붙이는 코드가 있어야 한다 (LICENSE·NOTICE)
_src = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "app.py"), encoding="utf-8").read()
assert "wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==" in _src and "signed(" in _src and "X-Author" in _src, "저작권 표기 누락"

print("selftest OK")
