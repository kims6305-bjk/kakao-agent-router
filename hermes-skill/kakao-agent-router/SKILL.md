---
name: kakao-agent-router
description: "Use when running or extending a KakaoTalk bot that calls Hermes from chat. Setup, owner gate, interview-added skills."
---

# kakao-agent-router (Hermes 운영 스킬)

카카오톡 방에서 주인이 `/에이전트 할 일`로 Hermes를 원샷 호출하고, `/기능추가` 인터뷰로
새 Hermes 스킬을 만들어 쓰는 라우터를 설치·운영할 때 쓴다. 레포 README가 정본이다.

## 설치 확인 순서
1. `agent-kakaotalk whoami`가 봇 전용 계정의 user_id를 돌려주는지 확인한다 (본계정 금지).
2. `config.example.json` → `config.json` 복사, `rooms` / `owner_chat_id` / `owner_user_id` 채우기.
   chat_id는 `agent-kakaotalk chat list`, user_id는 주인이 보낸 메시지의 `author_id`로 확인한다.
3. `python3 tests/test_router.py`가 `ALL OK`인지 본다.
4. `python3 router.py --once`로 기준선을 잡는다 (과거 메시지는 실행하지 않는다).
5. 상시 실행은 launchd/systemd 등 OS 서비스로 `python3 router.py`를 띄운다.

## 인터뷰로 추가된 스킬
- 위치: `config.skills_dir` (기본 `~/.hermes/skills/kakao-added/<이름>/SKILL.md`).
- 호출: 라우터가 `hermes -s <이름> -z "<요청>"`로 실행한다.
- 승인 전 초안은 `data/pending/<id>/`에만 있다. 설치는 주인의 `/승인 <id>`뿐이다.
- 스킬을 고칠 때는 파일을 직접 편집하고, 지우려면 그 디렉터리를 휴지통으로 옮긴다.

## 지키는 것
- 명령 권한 = 주인 방 AND 주인 user_id. 둘 중 하나만 맞으면 무시한다.
- `hermes -z`는 승인 프롬프트를 자동 통과하므로, 주인 이외의 입력이 이 경로에 닿게 바꾸지 않는다.
- 같은 이름의 기능은 덮어쓰지 않는다.
