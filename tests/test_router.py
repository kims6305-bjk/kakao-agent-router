"""표준 라이브러리만 쓰는 회귀 테스트: python3 tests/test_router.py"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router import Router  # noqa: E402

OWNER_ROOM, OWNER, BOT, STRANGER, OTHER_ROOM = "100", "1", "999", "2", "200"


class FakeTransport:
    def __init__(self):
        self.inbox = {OWNER_ROOM: [], OTHER_ROOM: []}
        self.sent = []
        self.n = 0

    def my_user_id(self):
        return BOT

    def push(self, room, author, text):
        self.n += 1
        self.inbox[room].append({"log_id": str(self.n), "author_id": author, "message": text})

    def fetch(self, room, since=None):
        return [m for m in self.inbox[room] if not since or int(m["log_id"]) > int(since)]

    def send(self, room, text):
        self.sent.append((room, text))
        return True


def make(backend, tmp, **extra):
    fake_bin = tmp / "fake_agent.sh"  # 받은 인자를 그대로 돌려주는 가짜 에이전트
    fake_bin.write_text('#!/bin/sh\necho "ARGS:$*"\n')
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)
    cfg = {"rooms": [OWNER_ROOM, OTHER_ROOM], "owner_chat_id": OWNER_ROOM, "owner_user_id": OWNER,
           "backend": backend, "hermes_bin": str(fake_bin), "skills_dir": str(tmp / "skills"),
           "plugins_dir": str(tmp / "plugins"), "data_dir": str(tmp / "data"), **extra}
    if backend == "python" and "agent_cmd" not in extra:
        cfg["agent_cmd"] = [str(fake_bin)]
    t = FakeTransport()
    return Router(cfg, t, base=tmp), t


def say(r, t, room, author, text):
    t.push(room, author, text)
    t.sent.clear()
    r.poll_once()
    return "\n".join(s for _, s in t.sent)


def interview(r, t, name):
    say(r, t, OWNER_ROOM, OWNER, "/기능추가")
    for ans in (name, "디스크 용량을 알려준다", "디스크 확인해줘", "없음", "남은 GB가 나오면 성공"):
        out = say(r, t, OWNER_ROOM, OWNER, ans)
    return out


def test_hermes_flow():
    tmp = Path(tempfile.mkdtemp())
    r, t = make("hermes", tmp)
    t.push(OWNER_ROOM, OWNER, "/에이전트 과거 메시지")  # 첫 실행 전 메시지 = 재생 금지
    r.poll_once()
    assert t.sent == [], t.sent

    assert say(r, t, OTHER_ROOM, STRANGER, "/에이전트 rm -rf") == ""          # 남의 방
    assert say(r, t, OWNER_ROOM, STRANGER, "/에이전트 비밀 알려줘") == ""      # 주인 방의 남
    assert say(r, t, OWNER_ROOM, BOT, "/에이전트 내 메시지") == ""             # 봇 자신

    out = say(r, t, OWNER_ROOM, OWNER, "/에이전트 디스크 확인")
    assert "ARGS:-z" in out and "디스크 확인" in out, out

    say(r, t, OWNER_ROOM, OWNER, "/기능추가")
    assert "영어 소문자" in say(r, t, OWNER_ROOM, OWNER, "Bad Name!")         # 이름 검증
    say(r, t, OWNER_ROOM, OWNER, "/취소")
    assert "interview" not in r.state

    out = interview(r, t, "disk-report")
    draft = out.split("id ")[1][:6]
    assert "name: disk-report" in out and f"/승인 {draft}" in out, out
    assert not (tmp / "skills/disk-report").exists()                          # 승인 전엔 미설치
    assert say(r, t, OWNER_ROOM, STRANGER, f"/승인 {draft}") == ""             # 남은 승인 불가
    assert not (tmp / "skills/disk-report").exists()
    say(r, t, OWNER_ROOM, OWNER, f"/승인 {draft}")
    skill = (tmp / "skills/disk-report/SKILL.md").read_text()
    assert skill.startswith("---\nname: disk-report\n") and "남은 GB" in skill

    out = say(r, t, OWNER_ROOM, OWNER, "/disk-report 지금")
    assert "-s disk-report" in out and "-z" in out, out
    assert "disk-report" in say(r, t, OWNER_ROOM, OWNER, "/기능목록")

    say(r, t, OWNER_ROOM, OWNER, "/기능추가")                                   # 중복 이름
    assert "이미 있어요" in say(r, t, OWNER_ROOM, OWNER, "disk-report")
    say(r, t, OWNER_ROOM, OWNER, "/취소")

    routes = [json.loads(l)["route"] for l in open(tmp / "data/routes.jsonl")]
    assert routes.count("not_owner") == 3 and "approve" in routes and "feature:disk-report" in routes, routes
    print("hermes flow OK")


def test_python_flow():
    tmp = Path(tempfile.mkdtemp())
    r, t = make("python", tmp)
    r.poll_once()
    out = interview(r, t, "disk-report")
    draft = out.split("id ")[1][:6]
    assert "def run(task, ctx)" in out
    say(r, t, OWNER_ROOM, OWNER, f"/승인 {draft}")
    assert (tmp / "plugins/disk-report.py").exists()
    out = say(r, t, OWNER_ROOM, OWNER, "/disk-report 지금")
    assert out.startswith("ARGS:기능 명세:") and "지금" in out, out

    (tmp / "plugins/broken.py").write_text("def run(task, ctx):\n    raise ValueError('boom')\n")
    assert "ValueError" in say(r, t, OWNER_ROOM, OWNER, "/broken x")           # 플러그인 오류 격리

    r2, t2 = make("python", Path(tempfile.mkdtemp()), agent_cmd=None)
    r2.cfg.pop("agent_cmd")
    r2.poll_once()
    assert "agent_cmd" in say(r2, t2, OWNER_ROOM, OWNER, "/에이전트 아무거나")
    print("python flow OK")


def test_reject():
    tmp = Path(tempfile.mkdtemp())
    r, t = make("hermes", tmp)
    r.poll_once()
    draft = interview(r, t, "temp-tool").split("id ")[1][:6]
    say(r, t, OWNER_ROOM, OWNER, f"/거절 {draft}")
    assert not (tmp / "data/pending" / draft).exists() and r.installed() == []
    assert "찾지 못했어요" in say(r, t, OWNER_ROOM, OWNER, "/승인 ab12cd")
    assert say(r, t, OWNER_ROOM, OWNER, "/승인 ../../x") == ""                 # 경로 조작은 명령으로 안 잡힘
    print("reject OK")


if __name__ == "__main__":
    test_hermes_flow()
    test_python_flow()
    test_reject()
    print("ALL OK")
