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

shutil.rmtree(app.WS)
print("selftest OK")
