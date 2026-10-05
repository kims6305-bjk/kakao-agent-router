"""kakao-agent-router — 카톡 방에서 주인이 에이전트에게 일을 시키고(툴콜),
인터뷰로 새 기능(스킬/플러그인)을 추가하는 최소 라우터.

실행:  python3 router.py --config config.json
"""
import argparse
import importlib.util
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_REPLY = 3000

QUESTIONS = [
    ("name", "1/5 기능 이름을 영어 소문자·숫자·하이픈으로 정해주세요. (예: disk-report)"),
    ("what", "2/5 이 기능이 하는 일을 한두 문장으로 설명해주세요."),
    ("when", "3/5 언제 쓰나요? 이렇게 말하면 실행되면 좋겠다는 예시를 적어주세요."),
    ("inputs", "4/5 필요한 입력·파일·도구가 있나요? (없으면 '없음')"),
    ("done", "5/5 어떤 결과가 나오면 성공인가요?"),
]


def now():
    return datetime.now(timezone.utc).isoformat()


class Router:
    def __init__(self, cfg, transport, base=HERE):
        self.cfg = cfg
        self.t = transport
        self.data = (base / cfg.get("data_dir", "data")).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state.setdefault("last", {})
        self.backend = cfg.get("backend", "hermes")
        assert self.backend in ("hermes", "python"), self.backend
        self.skills_dir = Path(cfg.get("skills_dir", "~/.hermes/skills/kakao-added")).expanduser()
        self.plugins_dir = (base / cfg.get("plugins_dir", "plugins")).resolve()
        self.me = None

    # ── 공통 ──────────────────────────────────────────────
    def save(self):
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def log_route(self, chat_id, log_id, route):
        with open(self.data / "routes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now(), "chat_id": str(chat_id), "log_id": str(log_id), "route": route}) + "\n")

    def reply(self, chat_id, text):
        if len(text) > MAX_REPLY:
            text = text[:MAX_REPLY] + "\n…(길어서 잘랐어요)"
        self.t.send(chat_id, text)

    def is_owner(self, chat_id, author_id):
        # 방과 사람 둘 다 맞아야 주인. 하나만 맞으면 남이 같은 방에 있을 때 뚫린다.
        return (str(chat_id) == str(self.cfg["owner_chat_id"])
                and str(author_id) == str(self.cfg["owner_user_id"]))

    # ── 루프 ──────────────────────────────────────────────
    def poll_once(self):
        if self.me is None:
            self.me = self.t.my_user_id()
        for room in self.cfg["rooms"]:
            room = str(room)
            since = self.state["last"].get(room)
            msgs = sorted(self.t.fetch(room, since), key=lambda m: int(m.get("log_id", 0)))
            if since is None:  # 첫 실행: 과거 메시지는 재생하지 않고 기준선만 잡는다
                # 빈 방도 ""로 기준선 완료 표시. 안 그러면 첫 실제 메시지가 기준선으로 삼켜진다.
                self.state["last"][room] = str(msgs[-1]["log_id"]) if msgs else ""
                self.save()
                continue
            for m in msgs:
                self.state["last"][room] = str(m["log_id"])
                self.save()  # 처리 전에 저장: 크래시해도 같은 명령을 두 번 실행하지 않는다
                if str(m.get("author_id")) == str(self.me):
                    continue
                self.handle(room, m)

    def handle(self, chat_id, m):
        text = (m.get("message") or "").strip()
        log_id = m.get("log_id")
        if not self.is_owner(chat_id, m.get("author_id")):
            self.log_route(chat_id, log_id, "not_owner")
            return self.handle_other(chat_id, m)
        route = self.owner_command(chat_id, text)
        self.log_route(chat_id, log_id, route)

    def handle_other(self, chat_id, m):
        """주인이 아닌 메시지. 기본은 무시. Q&A 봇을 붙이려면 여기에 답변 로직을 넣는다."""
        return None

    # ── 주인 명령 ─────────────────────────────────────────
    def owner_command(self, chat_id, text):
        iv = self.state.get("interview")
        if text == "/취소":
            self.state.pop("interview", None)
            self.save()
            self.reply(chat_id, "인터뷰를 취소했어요.")
            return "interview_cancel"
        if iv:
            return self.interview_step(chat_id, text)
        if text == "/기능추가":
            self.state["interview"] = {"answers": {}}
            self.save()
            self.reply(chat_id, f"새 기능 인터뷰를 시작할게요. 그만두려면 /취소\n\n{QUESTIONS[0][1]}")
            return "interview_start"
        m = re.match(r"^/(승인|거절)\s+(\w+)$", text)
        if m:
            return self.decide(chat_id, m.group(1), m.group(2))
        if text == "/기능목록":
            self.reply(chat_id, "설치된 기능: " + (", ".join(self.installed()) or "없음"))
            return "list"
        m = re.match(r"^/(\S+)\s*(.*)$", text, re.S)
        if m:
            cmd, task = m.group(1), m.group(2).strip()
            if cmd in ("에이전트", "agent"):
                self.reply(chat_id, self.run_agent(task))
                return "agent"
            if cmd in self.installed():
                self.reply(chat_id, self.run_feature(cmd, task))
                return f"feature:{cmd}"
        return "owner_chat"

    # ── 인터뷰 ────────────────────────────────────────────
    def interview_step(self, chat_id, text):
        iv = self.state["interview"]
        key, _ = QUESTIONS[len(iv["answers"])]
        if key == "name":
            if not NAME_RE.match(text):
                self.reply(chat_id, "이름은 영어 소문자·숫자·하이픈 2~40자로 해주세요. (예: disk-report)")
                return "interview_retry"
            if text in self.installed() or text in ("에이전트", "agent"):
                self.reply(chat_id, f"'{text}'는 이미 있어요. 다른 이름으로 해주세요.")
                return "interview_retry"
        iv["answers"][key] = text
        if len(iv["answers"]) < len(QUESTIONS):
            self.save()
            self.reply(chat_id, QUESTIONS[len(iv["answers"])][1])
            return "interview_step"
        draft_id = secrets.token_hex(3)
        d = self.data / "pending" / draft_id
        d.mkdir(parents=True)
        a = iv["answers"]
        (d / "answers.json").write_text(json.dumps(a, ensure_ascii=False, indent=1))
        body = render_skill(a) if self.backend == "hermes" else render_plugin(a)
        (d / ("SKILL.md" if self.backend == "hermes" else "plugin.py")).write_text(body)
        self.state.pop("interview")
        self.save()
        self.reply(chat_id, f"초안을 만들었어요 (id {draft_id}, {self.backend} 방식).\n\n"
                            f"{body[:1500]}\n\n켜려면 /승인 {draft_id}, 버리려면 /거절 {draft_id}")
        return "interview_done"

    def decide(self, chat_id, verb, draft_id):
        d = self.data / "pending" / draft_id
        if not re.fullmatch(r"[0-9a-f]{6}", draft_id) or not d.is_dir():
            self.reply(chat_id, f"초안 {draft_id}를 찾지 못했어요.")
            return "decide_missing"
        if verb == "거절":
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            self.reply(chat_id, f"초안 {draft_id}를 버렸어요.")
            return "reject"
        name = json.loads((d / "answers.json").read_text())["name"]
        if name in self.installed():
            self.reply(chat_id, f"'{name}'는 이미 설치돼 있어서 덮어쓰지 않았어요.")
            return "approve_conflict"
        if self.backend == "hermes":
            dst = self.skills_dir / name
            dst.mkdir(parents=True)
            (d / "SKILL.md").rename(dst / "SKILL.md")
        else:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            (d / "plugin.py").rename(self.plugins_dir / f"{name}.py")
        self.reply(chat_id, f"'{name}' 기능을 켰어요. 쓰는 법: /{name} 할 일")
        return "approve"

    def installed(self):
        if self.backend == "hermes":
            return sorted(p.parent.name for p in self.skills_dir.glob("*/SKILL.md"))
        return sorted(p.stem for p in self.plugins_dir.glob("*.py") if not p.name.startswith("_"))

    # ── 실행 백엔드 ───────────────────────────────────────
    def run_agent(self, task, skill=None):
        if not task:
            return "할 일을 같이 적어주세요. 예: /에이전트 디스크 남은 용량 알려줘"
        if self.backend == "python":
            cmd = self.cfg.get("agent_cmd")
            if not cmd:
                return "python 방식에서는 config의 agent_cmd를 설정해야 /에이전트를 쓸 수 있어요."
            return call(cmd + [PROMPT.format(task=task)], self.cfg)
        cmd = [self.cfg.get("hermes_bin", "hermes")]
        if skill:
            cmd += ["-s", skill]
        return call(cmd + ["-z", PROMPT.format(task=task)], self.cfg)

    def run_feature(self, name, task):
        if self.backend == "hermes":
            return self.run_agent(task or "이 스킬의 기본 동작을 실행해", skill=name)
        spec = importlib.util.spec_from_file_location(f"plugin_{name}", self.plugins_dir / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            return str(mod.run(task, {"agent_cmd": self.cfg.get("agent_cmd"), "timeout": self.cfg.get("timeout", 420)}))
        except Exception as e:  # 플러그인 오류가 라우터를 죽이지 않게
            return f"'{name}' 실행 중 오류: {type(e).__name__}: {e}"


PROMPT = ("카카오톡으로 받은 작업 요청이다. 필요한 도구를 실제로 실행해 결과를 확인한 뒤, "
          "카톡에 보낼 짧은 한국어 평문으로 답하라. 마크다운 금지, 3000자 이내, 비밀값 노출 금지.\n\n요청: {task}")


def call(cmd, cfg):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.get("timeout", 420))
    except subprocess.TimeoutExpired:
        return "시간이 너무 오래 걸려서 중단했어요."
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        return f"실행에 실패했어요 (rc={p.returncode})."
    return out


def render_skill(a):
    one = lambda s: " ".join(s.split()).replace('"', "'")
    return f"""---
name: {a['name']}
description: "Use when {one(a['when'])[:120]}. {one(a['what'])[:120]}"
---

# {a['name']}

카카오톡 인터뷰로 만든 스킬. 라우터가 `/{a['name']} 할 일`로 호출한다.

## 하는 일
{a['what']}

## 언제 쓰나 (트리거 예시)
{a['when']}

## 입력·도구
{a['inputs']}

## 완료 기준
{a['done']}
결과는 실제 실행한 도구 출력에 근거해 보고하고, 못 한 것은 못 했다고 말한다.

## 안전
- 비밀값(토큰·비밀번호)을 답변에 넣지 않는다.
- 외부 발송·삭제·결제처럼 되돌릴 수 없는 행동은 실행하지 말고 무엇을 하려는지만 답한다.
"""


def render_plugin(a):
    return f'''"""카카오톡 인터뷰로 만든 플러그인: {a["name"]}
run()을 직접 구현해도 되고, 그대로 두면 config의 agent_cmd(예: claude -p, codex exec)에
아래 SPEC을 붙여 넘긴다."""
import subprocess

SPEC = {json.dumps(a, ensure_ascii=False, indent=4)}


def run(task, ctx):
    cmd = ctx.get("agent_cmd")
    if not cmd:
        return "아직 구현되지 않은 기능이에요. plugins/{a["name"]}.py의 run()을 채우거나 agent_cmd를 설정해 주세요."
    prompt = (f"기능 명세: {{SPEC}}\\n요청: {{task}}\\n"
              "명세의 완료 기준대로 실제로 실행하고, 짧은 한국어 평문으로 결과만 답하라. 비밀값 노출 금지.")
    p = subprocess.run(cmd + [prompt], capture_output=True, text=True, timeout=ctx.get("timeout", 420))
    return (p.stdout or "").strip() or f"실행 실패 (rc={{p.returncode}})"
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    from transport import AgentKakaotalkCLI
    r = Router(cfg, AgentKakaotalkCLI(cfg.get("kakao_bin", "agent-kakaotalk")))
    while True:
        try:
            r.poll_once()
        except Exception as e:  # 네트워크·세션 오류로 죽지 않고 다음 주기에 재시도
            print(f"[{now()}] poll error: {e}", file=sys.stderr, flush=True)
        if a.once:
            break
        time.sleep(cfg.get("poll_interval", 20))


if __name__ == "__main__":
    main()
