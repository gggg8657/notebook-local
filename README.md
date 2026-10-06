# notebook-local — 문서 올리면 출처 인용 채팅·노트·요약·FAQ·팟캐스트 (NotebookLM 클론)

> **한 줄 요약** — Google NotebookLM 을 폐쇄망용으로 가볍게 다시 만든 패키지입니다. HWP·HWPX·PDF·DOCX·XLSX·이미지(OCR)를 노트북에 올리면
> 로컬 LLM 이 **출처 번호 `[1][2]` 를 달아** 답하고(클릭하면 발췌 카드), 답을 노트로 저장하고, 버튼 하나로 **전체 요약·FAQ·스터디 가이드·타임라인·브리핑·팟캐스트 대본**을 만듭니다.
> 검색은 임베딩(bge-m3 등) + BM25 하이브리드이고 임베딩 서버가 없으면 BM25 만으로 동작합니다. 팟캐스트는 TTS 서버(Kokoro 등 OpenAI 호환)가 있으면 mp3 까지 만듭니다.
> 파이썬은 표준 라이브러리만(sqlite 저장), 문서 파서는 [kordoc](https://github.com/chrisryugj/kordoc)(MIT, Node 20). 클라우드 API·API 키 없음.
> 로컬 8B 모델로 PoC 를 돌려 인용·요약·팟캐스트 대본까지 확인했고, GPU 서버에서 큰 모델을 붙이면 답변 품질이 올라갑니다.
>
> - **설치**: `bash setup.sh` 하나. 폐쇄망은 `pack.sh` 번들 반입 + 서버에 `ollama pull bge-m3`(없으면 BM25).
> - **외부 통신**: 없음. LLM·임베딩·TTS 서버 주소만 환경변수로.
> - **모델**: 한국어 되는 아무거나. Ollama·vLLM(OpenAI 호환) 전환은 환경변수 하나.

## 실행

```bash
bash setup.sh                     # OS → Python → Node·kordoc → LLM 탐색 → 임베딩 모델 → selftest → http://localhost:8769
bash setup.sh stop
npm install && python3 selftest.py && python3 app.py                       # 수동
LLM_API=openai LLM_BASE_URL=http://gpu:8000/v1 LLM_MODEL=Qwen3-32B EMBED_MODEL=bge-m3 python3 app.py
python3 app.py --cli add 노트북 문서.hwpx
python3 app.py --cli ask 노트북 "예산은 얼마인가"
python3 app.py --cli studio 노트북 podcast
```

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `LLM_API` | `ollama` | `ollama` 또는 `openai` |
| `LLM_BASE_URL` | `http://localhost:11434` / `http://localhost:8000/v1` | LLM 서버 |
| `LLM_MODEL` | `qwen3:8b` | 답변 모델 (UI 에서 변경 가능) |
| `LLM_API_KEY` | (없음) | OpenAI 호환 서버 키 |
| `EMBED_MODEL` | `bge-m3` | 임베딩 모델 (Ollama `/api/embed` 또는 `/v1/embeddings`). 실패하면 BM25 전용 |
| `TTS_BASE_URL` | (없음) | OpenAI 호환 `/v1/audio/speech` 서버 (예 `http://localhost:8771/v1`). 있으면 팟캐스트 mp3 생성 |
| `TTS_MODEL` / `TTS_VOICE_A` / `TTS_VOICE_B` | `kokoro` / `af_heart` / `am_adam` | TTS 모델·진행자 A/B 목소리 |
| `TOP_K` / `MAX_CHARS` / `NUM_CTX` | `8` / `20000` / `16384` | 검색 발췌 수 / 스튜디오 입력 상한 / Ollama 컨텍스트 |
| `PORT` | `8769` | |
| `KORDOC_OFFLINE` | (없음) | `1` 이면 kordoc 아웃바운드 차단 (폐쇄망 권장) |

## 파이프라인

```
출처 추가  kordoc parse(문서→MD) → 청크(헤딩 breadcrumb, ~800자, 겹침 100) → 임베딩 or BM25 → sqlite → 3줄 요약(LLM 1콜)
질문      BM25 + 코사인 → RRF 융합 상위 8 → [n] 번호 붙여 LLM 1콜 → 인용 달린 답(스트리밍) → 노트 저장
스튜디오  포함된 출처 본문(상한 MAX_CHARS) → LLM 1콜 → 노트 저장. 팟캐스트는 A/B 대본 → (TTS 서버) 줄별 합성 → ffmpeg 연결 → mp3
```

데이터는 `_workspace/notebook.db` 와 `_workspace/<노트북>/<출처>/source.md`, 오디오는 `_workspace/<노트북>/audio/`.

## 폐쇄망 반입

```bash
./pack.sh linux-x64            # node_modules(kordoc)+OCR 모델+스크립트 → dist-offline/notebook-local-linux-x64.tar.gz
WITH_NODE=1 ./pack.sh          # Node 바이너리까지
```

서버에서 `KORDOC_OFFLINE=1 LLM_BASE_URL=http://<gpu>:11434 bash setup.sh`. 임베딩 모델(`bge-m3`, 1.2GB)은 서버 Ollama 에 미리 반입하거나 없으면 BM25 로 동작.
`selftest.py` 는 LLM·임베딩 없이 청크·검색·인용·노트·kordoc 파싱을 검증하므로 반입 직후 점검용.

## 파일

`app.py` 서버+파이프라인 · `ui.html` · `goal-prompt.md` 역할 프롬프트(요약·채팅·스튜디오 6종) · `selftest.py` · `setup.sh` · `pack.sh` · `package.json`(kordoc ^4.17). 출처·라이선스는 `NOTICE`.

### ✍ 윤문하기
노트 편집 창·스튜디오 결과 아래에 "윤문하기" 막대(가볍게·보통·적극). kordoc-local 의 글 윤문 API(`KORDOC_URL`, 기본 `http://localhost:8766`)를 부르며, kordoc 이 안 떠 있으면 막대가 숨는다. 숫자·날짜·고유 표기가 바뀐 곳은 원문 유지, "바뀐 곳 보기"로 어절 단위 비교.

## 출처·감사 (Credits)

- [kordoc](https://github.com/chrisryugj/kordoc) (MIT, chrisryugj) — 문서 파싱·OCR (npm 에서 수정 없이 설치, `LICENSE-kordoc`)
- 기획 참고: Google NotebookLM (코드 공유 없음). 임베딩(예: [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3), MIT)·TTS 서버(예: Kokoro, Apache-2.0, 또는 [tts-local](https://github.com/gggg8657/tts-local))는 환경변수로 연결하는 외부 서비스
- **LLM 실행** — OpenAI 호환 API 로 호출합니다(모델 가중치는 동봉하지 않음). 기본 배포는 [Ollama](https://github.com/ollama/ollama) (MIT) 위의 Google [Gemma](https://ai.google.dev/gemma) `gemma4:31b` — 모델 이용 조건은 Gemma 배포처 참고.
- 이 도구는 [agent-page-portal](https://github.com/gggg8657/agent-page-portal) 에 연결해 쓰도록 만들었습니다(단독 실행도 됨).

저작권 표기·전체 목록은 `NOTICE` 를 보세요.
