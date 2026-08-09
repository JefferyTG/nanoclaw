#!/usr/bin/env python3
"""邮箱新邮件检查工具（IMAP）。

用法：
    python scripts/email_check.py                 # 检查所有配置的邮箱（默认）
    python scripts/email_check.py --account 163   # 只检查指定账号
    python scripts/email_check.py --since-days 3  # 忽略状态文件，看最近 N 天（调试用）
    python scripts/email_check.py --list --since-days 7   # 列出最近邮件（含 UID，供后续操作）
    python scripts/email_check.py --show <uid>    # 查看指定 UID 邮件的正文
    python scripts/email_check.py --mark-read <uid> [uid...]  # 把指定 UID 邮件标记为已读

配置：
    workspace/email_accounts.json  邮箱账号与授权码（已 gitignore，勿提交）
    workspace/email_state.json     检查状态（记录每个账号已读到的最大 UID）

输出：
    检查模式打印人类可读的新邮件摘要；无新邮件打印 "NO_NEW_MAIL"。
    任一步失败打印 "ERROR: ..."（不退出码报错，方便 agent 兜底）。

依赖：仅 Python 标准库（imaplib / email）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime

# 向上查找项目根（含 workspace 的目录）；脚本可放在 scripts/ 或 skills/*/scripts/
def _find_project_root() -> str:
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):  # 最多上溯 6 层
        if os.path.isdir(os.path.join(cur, "workspace")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = _find_project_root()
ACCOUNTS_FILE = os.path.join(BASE_DIR, "workspace", "email_accounts.json")
STATE_FILE = os.path.join(BASE_DIR, "workspace", "email_state.json")

# 常见邮箱 IMAP 服务器（SSL）
KNOWN_SERVERS = {
    "163": ("imap.163.com", 993),
    "126": ("imap.126.com", 993),
    "qq": ("imap.qq.com", 993),
    "gmail": ("imap.gmail.com", 993),
}


def load_accounts() -> list[dict]:
    """读取邮箱账号配置；文件缺失或格式错误返回空列表。"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("accounts", []) if isinstance(data, dict) else []
        return [a for a in accounts if isinstance(a, dict) and a.get("enabled", True)]
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: 读取邮箱配置失败: {exc}")
        return []


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"WARN: 无法写入状态文件: {exc}")


def resolve_server(account: dict) -> tuple[str, int]:
    """解析 IMAP 服务器；优先账号内显式 host/port，其次按 provider 查表。"""
    host = account.get("host") or account.get("imap_host")
    port = int(account.get("port") or account.get("imap_port") or 993)
    if host:
        return host, port
    provider = (account.get("provider") or "").lower()
    if provider in KNOWN_SERVERS:
        return KNOWN_SERVERS[provider]
    # 兜底：从邮箱地址猜域名
    email = account.get("email", "")
    if "@" in email:
        domain = email.split("@", 1)[1].lower()
        if "163" in domain:
            return KNOWN_SERVERS["163"]
        if "126" in domain:
            return KNOWN_SERVERS["126"]
        if "qq" in domain:
            return KNOWN_SERVERS["qq"]
        return f"imap.{domain}", 993
    raise ValueError("无法确定 IMAP 服务器，请在配置里写 host")


def decode_mime(value: str) -> str:
    """解码邮件头（兼容 =?utf-8?B?...?= 编码）。"""
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def connect(account: dict):
    """连接并登录账号，返回 (conn, label)。"""
    import imaplib

    email_addr = account.get("email", "")
    auth_code = account.get("auth_code", "")
    if not email_addr or not auth_code:
        print(f"WARN: 账号 {email_addr or '未知'} 缺少 email 或 auth_code，跳过")
        return None, None

    host, port = resolve_server(account)
    label = account.get("name") or email_addr
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as exc:
        print(f"ERROR: {label} 连接 {host}:{port} 失败: {exc}")
        return None, None

    try:
        conn.login(email_addr, auth_code)
    except imaplib.IMAP4.error as exc:
        print(f"ERROR: {label} 登录失败（请检查授权码是否正确）: {exc}")
        try:
            conn.logout()
        except Exception:
            pass
        return None, None
    return conn, label


def _extract_uid(meta: bytes) -> int:
    """从 fetch 返回的元数据行提取 UID。"""
    meta_str = meta.decode("utf-8", errors="replace")
    if "UID" not in meta_str:
        return 0
    try:
        return int(meta_str.split("UID", 1)[1].split(")", 1)[0].strip().split()[0])
    except (ValueError, IndexError):
        return 0


def parse_headers(header_raw: bytes) -> dict:
    import email as email_lib
    from email import policy

    msg = email_lib.message_from_bytes(header_raw, policy=policy.default)
    sender = decode_mime(str(msg.get("From", "")))
    subject = decode_mime(str(msg.get("Subject", ""))) or "(无主题)"
    date_str = str(msg.get("Date", "")).strip()
    sender_short = sender
    if "<" in sender:
        name_part = sender.split("<", 1)[0].strip()
        addr_part = sender.split("<", 1)[1].rstrip(">").strip()
        sender_short = name_part or addr_part
    return {"from": sender_short, "subject": subject, "date": date_str}


def get_text_body(msg) -> str:
    """从邮件 Message 对象提取纯文本正文（优先 text/plain，其次 html 去标签）。"""
    if msg.is_multipart():
        # 优先 text/plain
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not part.get_filename():
                try:
                    return part.get_content()
                except Exception:
                    return ""
        # 回退 text/html（去标签）
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                try:
                    html = part.get_content()
                    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
                    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
                    html = re.sub(r"<br\s*/?>", "\n", html)
                    html = re.sub(r"</p>", "\n", html)
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text)
                    return text.strip()
                except Exception:
                    return ""
    else:
        try:
            return msg.get_content()
        except Exception:
            return ""
    return ""


def search_recent(conn, since_days: int):
    """搜索最近 N 天的邮件序列号列表（只读）。"""
    since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    typ, data = conn.search(None, "SINCE", since_date)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def list_recent(account: dict, since_days: int) -> list[dict]:
    """列出最近邮件（含 UID、发件人、主题）。"""
    import imaplib

    conn, label = connect(account)
    if conn is None:
        return []
    try:
        conn.select("INBOX", readonly=True)
        ids = search_recent(conn, since_days)
        ids = ids[-30:]  # 最多 30 封
        mails = []
        for num in ids:
            try:
                typ, msg_data = conn.fetch(
                    num, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
            except imaplib.IMAP4.error:
                continue
            if typ != "OK" or not msg_data:
                continue
            uid, header_raw = 0, b""
            for part in msg_data:
                if isinstance(part, tuple):
                    meta, body = part
                    if isinstance(meta, bytes):
                        uid = _extract_uid(meta) or uid
                    if isinstance(body, bytes):
                        header_raw += body
            if uid:
                info = parse_headers(header_raw)
                mails.append({"uid": uid, **info})
        return mails
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def show_email(account: dict, uid: int) -> None:
    """查看指定 UID 邮件的正文。"""
    import imaplib
    import email as email_lib
    from email import policy

    conn, label = connect(account)
    if conn is None:
        return
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            print(f"ERROR: {label} 找不到 UID={uid} 的邮件")
            return
        raw = b""
        for part in data:
            if isinstance(part, tuple) and isinstance(part[1], bytes):
                raw += part[1]
        if not raw:
            print(f"ERROR: {label} UID={uid} 内容为空")
            return
        msg = email_lib.message_from_bytes(raw, policy=policy.default)
        sender = decode_mime(str(msg.get("From", "")))
        subject = decode_mime(str(msg.get("Subject", ""))) or "(无主题)"
        date_str = str(msg.get("Date", "")).strip()
        body = get_text_body(msg).strip()
        print(f"📧 来自: {sender}")
        print(f"📌 主题: {subject}")
        print(f"🕐 时间: {date_str}")
        print("─" * 30)
        if body:
            print(body[:3000])
        else:
            print("(无纯文本正文，可能是图片/HTML 邮件)")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def mark_read(account: dict, uids: list[int]) -> None:
    """把指定 UID 的邮件标记为已读（可写模式）。"""
    import imaplib

    conn, label = connect(account)
    if conn is None:
        return
    try:
        # 标记已读需要非只读模式
        conn.select("INBOX", readonly=False)
        ok, done = 0, 0
        for uid in uids:
            typ, _ = conn.uid("store", str(uid), "+FLAGS", r"(\Seen)")
            done += 1
            if typ == "OK":
                ok += 1
        print(f"✅ {label}: 已标记 {ok}/{done} 封邮件为已读")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def check_account(account: dict, state: dict, since_days: int | None) -> list[dict]:
    """检查单个邮箱，返回新邮件列表 [{uid, from, subject, date}]。"""
    import imaplib

    email_addr = account.get("email", "")
    auth_code = account.get("auth_code", "")
    if not email_addr or not auth_code:
        print(f"WARN: 账号 {email_addr or '未知'} 缺少 email 或 auth_code，跳过")
        return []

    conn, label = connect(account)
    if conn is None:
        return []

    try:
        conn.select("INBOX", readonly=True)

        # 确定要看的范围：优先状态文件的 last_uid；首次使用可 --since-days
        last_uid = state.get("last_uid")
        if since_days is not None:
            since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", since_date)
        elif last_uid:
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        else:
            since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", since_date)

        if typ != "OK" or not data or not data[0]:
            return []

        ids = data[0].split()
        if not ids:
            return []

        ids = ids[-20:]

        new_mails = []
        max_uid = last_uid or 0
        for num in ids:
            try:
                typ, msg_data = conn.fetch(num, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            except imaplib.IMAP4.error:
                continue
            if typ != "OK" or not msg_data:
                continue

            uid = 0
            header_raw = b""
            for part in msg_data:
                if isinstance(part, tuple):
                    meta, body = part
                    if isinstance(meta, bytes):
                        uid = _extract_uid(meta) or uid
                    if isinstance(body, bytes):
                        header_raw += body

            if since_days is None and last_uid is not None and uid <= last_uid:
                continue
            if uid > max_uid:
                max_uid = uid

            info = parse_headers(header_raw)
            new_mails.append({"uid": uid, **info})

        if since_days is None:
            if last_uid is None:
                state["last_uid"] = max_uid or 0
                state["first_seen_at"] = datetime.now().isoformat(timespec="seconds")
            elif max_uid > last_uid:
                state["last_uid"] = max_uid
            state["checked_at"] = datetime.now().isoformat(timespec="seconds")
        return new_mails

    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="邮箱检查工具（IMAP 只读 / 标已读）")
    parser.add_argument("--account", help="指定账号（name 或 email 关键字）")
    parser.add_argument("--since-days", type=int, help="忽略状态，看最近 N 天（调试/列表用）")
    parser.add_argument("--list", action="store_true", help="列出最近邮件（含 UID）")
    parser.add_argument("--show", type=int, metavar="UID", help="查看指定 UID 邮件的正文")
    parser.add_argument("--mark-read", nargs="+", type=int, metavar="UID", help="把指定 UID 邮件标记为已读")
    args = parser.parse_args()

    accounts = load_accounts()
    if not accounts:
        print("ERROR: 没有配置邮箱。请先在 workspace/email_accounts.json 填写账号与授权码。")
        return

    if args.account:
        key = args.account.lower()
        accounts = [
            a for a in accounts
            if key in (a.get("name", "") or "").lower()
            or key in (a.get("email", "") or "").lower()
        ]
        if not accounts:
            print(f"ERROR: 找不到账号 {args.account}")
            return

    # 操作模式：--show / --mark-read 只对第一个匹配账号操作（避免重复）
    target = accounts[0]

    if args.show is not None:
        show_email(target, args.show)
        return

    if args.mark_read:
        mark_read(target, args.mark_read)
        return

    if args.list:
        since = args.since_days or 7
        for acc in accounts:
            label = acc.get("name") or acc.get("email", "")
            mails = list_recent(acc, since)
            if not mails:
                print(f"📭 {label}: 最近 {since} 天无邮件")
                continue
            print(f"📬 {label}（最近 {since} 天 {len(mails)} 封）：")
            for m in mails:
                print(f"  UID {m['uid']:>6} | {m['from']} |《{m['subject']}》")
        return

    # 默认：检查新邮件
    state = load_state()
    all_new = []
    for acc in accounts:
        label = acc.get("name") or acc.get("email", "")
        mails = check_account(acc, state, args.since_days)
        if mails:
            all_new.append({"label": label, "mails": mails})

    save_state(state)

    if not all_new:
        print("NO_NEW_MAIL")
        return

    lines = []
    for group in all_new:
        lines.append(f"📬 {group['label']} 有 {len(group['mails'])} 封新邮件：")
        for m in group["mails"]:
            lines.append(f"  UID {m['uid']:>6} | 来自 {m['from']}：《{m['subject']}》")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
