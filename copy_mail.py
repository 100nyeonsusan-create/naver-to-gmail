"""네이버 받은메일함 -> 지메일 받은편지함 복사 (네이버는 읽기 전용).

- 네이버: EXAMINE(읽기 전용) + BODY.PEEK 만 사용. 삭제·이동·읽음 표시 안 함.
- 지메일: 같은 Message-ID 가 이미 있으면 건너뛰고, 없으면 원본 그대로 받은편지함에 넣음(APPEND).
- 로그인 정보는 환경 변수(NAVER_USER, NAVER_PASS, GMAIL_USER, GMAIL_PASS)로만 받음.
  환경 변수가 없으면(내 PC 시험용) 입력창으로 물어봄. 어디에도 저장하지 않음.
- DAYS: 최근 며칠치를 볼지(기본 2). SINCE_DATE(예: 25-Nov-2026)를 주면 그 날짜부터.
"""
import email.utils
import imaplib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 네이버 쪽 쓰기 명령 차단(실수 방지)
_BLOCK = {"STORE", "COPY", "MOVE", "EXPUNGE", "DELETE", "RENAME", "APPEND", "UID STORE", "UID COPY", "UID MOVE"}


class ReadOnlyIMAP(imaplib.IMAP4_SSL):
    def _command(self, name, *args):
        if name.upper() in _BLOCK:
            raise RuntimeError("네이버에는 쓰기 명령 금지: " + name)
        return super()._command(name, *args)

    def uid(self, command, *args):
        if command.upper() in ("STORE", "COPY", "MOVE", "EXPUNGE"):
            raise RuntimeError("네이버에는 쓰기 명령 금지: UID " + command)
        return super().uid(command, *args)


def ask(label, secret=True):
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
    v = simpledialog.askstring("메일 복사", label, show="*" if secret else None, parent=root)
    root.destroy()
    if not v:
        sys.exit("입력 취소")
    return v.strip()


def cred(name, label, secret=True):
    v = os.environ.get(name, "").strip()
    return v if v else ask(label, secret)


def log(*a):
    print(datetime.now(KST).strftime("%H:%M:%S"), *a, flush=True)


def all_mail_box(g):
    typ, data = g.list()
    for line in data or []:
        s = line.decode("utf-8", "replace")
        if "\\All" in s:
            m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"(\S+)\s*$", s)
            if m:
                return m.group(1)
    return "INBOX"


class GmailImap:
    """지메일 앱 비밀번호로 IMAP 에 직접 넣는 방식."""
    def __init__(self, user, pw):
        self.g = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        self.g.login(user, pw)
        box = all_mail_box(self.g)
        self.g.select('"%s"' % box if " " in box and not box.startswith('"') else box, readonly=True)

    def has(self, mid):
        typ, r = self.g.search(None, "X-GM-RAW", '"rfc822msgid:%s"' % mid)
        return typ == "OK" and bool(r and r[0].split())

    def put(self, raw, when):
        typ, _ = self.g.append("INBOX", None, imaplib.Time2Internaldate(when), raw)
        return typ == "OK"

    def logout(self):
        self.g.logout()


class Receiver:
    """100nyeonsusan 계정의 Apps Script 웹 앱(doPost)으로 보내는 방식(앱 비밀번호 불필요)."""
    def __init__(self, url):
        self.url = url

    def _post(self, payload):
        import base64, json, urllib.request
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError("받는 곳 오류: " + str(res.get("err")))
        return res

    def has(self, mid):
        return bool(self._post({"op": "has", "ids": [mid]})["has"][0])

    def put(self, raw, when):
        import base64
        self._post({"op": "put", "raw": base64.b64encode(raw).decode()})
        return True

    def logout(self):
        pass


def main():
    nu = cred("NAVER_USER", "네이버 아이디(예: 100nyeon85@naver.com)", False)
    np_ = cred("NAVER_PASS", "네이버 비밀번호(앱 비밀번호)")
    gas_url = os.environ.get("GAS_URL", "").strip()
    gu = gp = ""
    if not gas_url:
        gu = cred("GMAIL_USER", "지메일 주소", False)
        gp = cred("GMAIL_PASS", "지메일 앱 비밀번호").replace(" ", "")

    since = os.environ.get("SINCE_DATE", "").strip()
    if not since:
        days = int(os.environ.get("DAYS", "2") or 2)
        since = (datetime.now(KST) - timedelta(days=days)).strftime("%d-%b-%Y")

    g = Receiver(gas_url) if gas_url else GmailImap(gu, gp)
    loop_min = float(os.environ.get("LOOP_MINUTES", "0") or 0)   # 0 = 한 번만
    interval = int(os.environ.get("INTERVAL_SECONDS", "60") or 60)
    end = time.time() + loop_min * 60
    state = {"n": None, "last": 0}
    total_fail = 0
    while True:
        try:
            if state["n"] is None:
                n = ReadOnlyIMAP("imap.naver.com", 993)
                n.login(nu, np_)
                state["n"] = n
            n = state["n"]
            typ, _ = n.select("INBOX", readonly=True)  # EXAMINE (다시 열면 새 메일 반영)
            if typ != "OK":
                raise RuntimeError("네이버 받은메일함 열기 실패")
            if state["last"]:
                typ, data = n.uid("SEARCH", None, "UID", "%d:*" % (state["last"] + 1))
            else:
                typ, data = n.uid("SEARCH", None, "SINCE", since)
            uids = [u for u in (data[0].split() if data and data[0] else []) if int(u) > state["last"]]
            if uids or not state["last"]:
                f = copy_uids(n, g, uids)
                total_fail += f
            if uids:
                state["last"] = max(state["last"], max(int(u) for u in uids))
            elif not state["last"]:
                typ, d = n.uid("SEARCH", None, "ALL")
                allu = d[0].split() if d and d[0] else []
                state["last"] = int(allu[-1]) if allu else 0
        except Exception as e:
            log("연결 오류, 다시 연결:", type(e).__name__, str(e)[:120])
            try:
                state["n"].logout()
            except Exception:
                pass
            state["n"] = None
        if time.time() + interval > end:
            break
        time.sleep(interval)
    try:
        state["n"] and state["n"].logout()
    except Exception:
        pass
    if total_fail and not loop_min:
        sys.exit(1)


def copy_uids(n, g, uids):
    log(f"새 메일 {len(uids)}통 확인")
    copied = skipped = noid = failed = 0
    for uid in uids:
        try:
            typ, d = n.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            hdr = b""; idate = None
            for part in d:
                if isinstance(part, tuple):
                    hdr = part[1]
                    idate = imaplib.Internaldate2tuple(part[0])
            m = re.search(rb"Message-ID:\s*(<[^>\r\n]+>)", hdr, re.I)
            if not m:
                noid += 1
                continue
            mid = m.group(1).decode("utf-8", "replace").replace('"', "")
            if g.has(mid):
                skipped += 1
                continue
            typ, d = n.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in d if isinstance(p, tuple)), None)
            if not raw:
                failed += 1
                continue
            when = time.mktime(idate) if idate else time.time()
            if g.put(raw, when):
                copied += 1
            else:
                failed += 1
        except Exception as e:  # 한 통 실패해도 계속
            failed += 1
            log("실패:", type(e).__name__, str(e)[:120])

    log(f"복사 {copied} / 이미 있음 {skipped} / Message-ID 없음 {noid} / 실패 {failed}")
    return failed


if __name__ == "__main__":
    main()
