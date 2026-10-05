"""전송층: 카톡 메시지 읽기/보내기만 담당. 다른 연결 방식(Iris, openkakao-cli 등)으로
바꾸려면 fetch/send/my_user_id 세 메서드를 가진 클래스를 하나 더 만들면 된다."""
import json
import subprocess
import time


class AgentKakaotalkCLI:
    """agent-messenger 패키지의 `agent-kakaotalk` CLI(LOCO 프로토콜) 래퍼."""

    def __init__(self, bin_path="agent-kakaotalk", min_interval=1.0):
        self.bin = bin_path
        self.min_interval = min_interval  # 연속 호출 시 세션 충돌 완화
        self._last = 0.0

    def _cmd(self, *args):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = subprocess.run([self.bin, *args], capture_output=True, text=True, timeout=30)
        finally:
            self._last = time.monotonic()
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        return json.loads(r.stdout or "null")

    def my_user_id(self):
        return str((self._cmd("whoami") or {}).get("user_id", ""))

    def fetch(self, chat_id, since=None, count=50):
        args = ["message", "list", str(chat_id), "--count", str(count)]
        if since:
            args += ["--from", str(since)]
        msgs = self._cmd(*args) or []
        return [m for m in msgs if str(m.get("log_id")) != str(since)]

    def send(self, chat_id, text):
        r = self._cmd("message", "send", str(chat_id), text)
        return isinstance(r, dict) and r.get("success") is True
