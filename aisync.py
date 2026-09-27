#!/usr/bin/env python3
"""aisync - see and sync conversations between Claude Code and OpenCode.

Claude Code keeps sessions as JSONL files in ~/.claude/projects/<dir-slug>/<uuid>.jsonl.
OpenCode keeps sessions in a SQLite DB (~/.local/share/opencode/opencode.db) and
offers `opencode export` / `opencode import` for JSON round-trips.

aisync reads both, shows them in one list, and copies conversations across so you can
continue from either tool. Tool calls are carried over as readable text summaries
(not native tool calls) so neither side's API rejects the history.
"""
import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CC_PROJECTS = os.path.join(HOME, ".claude", "projects")
OC_DB = os.environ.get("OPENCODE_DB", os.path.join(HOME, ".local", "share", "opencode", "opencode.db"))
STATE_DIR = os.environ.get("AISYNC_HOME", os.path.join(HOME, ".aisync"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
TOOL_OUTPUT_LIMIT = 1500
MARKER = "aisync"

# ---------------------------------------------------------------- helpers


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def parse_iso(s):
    try:
        return int(datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000)
    except Exception:
        return 0


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def truncate(s, n=TOOL_OUTPUT_LIMIT):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "\n…[truncated %d chars]" % (len(s) - n)


def cc_slug(directory):
    return re.sub(r"[^A-Za-z0-9]", "-", directory)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"pairs": []}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def find_pair(state, oc=None, cc=None):
    for p in state["pairs"]:
        if (oc and p["oc"] == oc) or (cc and p["cc"] == cc):
            return p
    return None


def running(name):
    try:
        out = subprocess.run(["pgrep", "-fl", name], capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if "aisync" not in l]
    except Exception:
        return []


# OpenCode ID format: prefix_ + 12 hex chars (ms*0x1000+counter, bit-inverted for
# descending ids like sessions) + 14 random base62 chars.
_oc_last = [0, 0]
_B62 = string.digits + string.ascii_uppercase + string.ascii_lowercase


def oc_id(prefix, descending=False, ms=None):
    ms = ms or now_ms()
    if ms != _oc_last[0]:
        _oc_last[0], _oc_last[1] = ms, 0
    _oc_last[1] += 1
    v = ms * 0x1000 + _oc_last[1]
    if descending:
        v = ~v & 0xFFFFFFFFFFFF
    return "%s_%012x%s" % (prefix, v & 0xFFFFFFFFFFFF, "".join(random.choice(_B62) for _ in range(14)))


# ---------------------------------------------------------------- Claude Code side

SYS_TAG = re.compile(r"<(system-reminder|local-command-caveat|command-name|command-message|command-args|local-command-stdout)>.*?</\1>\s*", re.S)


def clean_user_text(t):
    return SYS_TAG.sub("", t).strip()


def cc_read_entries(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def cc_main_chain(entries):
    """Entries on the active branch (walk parentUuid back from the last message)."""
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    leaf = None
    for e in reversed(entries):
        if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
            leaf = e
            break
    chain, cur, seen = [], leaf, set()
    while cur and cur.get("uuid") not in seen:
        seen.add(cur["uuid"])
        if cur.get("type") in ("user", "assistant"):
            chain.append(cur)
        cur = by_uuid.get(cur.get("parentUuid"))
    chain.reverse()
    return chain


def cc_turns(path):
    """Convert a Claude Code session into neutral turns:
    [{role, text, ts, src_ids:[uuids]}]"""
    entries = cc_read_entries(path)
    turns = []
    tool_names = {}
    for e in cc_main_chain(entries):
        msg = e.get("message") or {}
        content = msg.get("content")
        ts = parse_iso(e.get("timestamp", "")) or now_ms()
        role = e["type"]
        texts, is_tool_result = [], False
        if isinstance(content, str):
            if role == "user":
                if e.get("isMeta"):
                    continue
                t = clean_user_text(content)
                if t:
                    texts.append(t)
            else:
                texts.append(content)
        elif isinstance(content, list):
            for b in content:
                bt = b.get("type")
                if bt == "text":
                    t = clean_user_text(b.get("text", "")) if role == "user" else b.get("text", "")
                    if t.strip():
                        texts.append(t)
                elif bt == "tool_use":
                    tool_names[b.get("id")] = b.get("name")
                    texts.append("[tool call: %s] %s" % (b.get("name"), truncate(b.get("input", {}), 800)))
                elif bt == "tool_result":
                    is_tool_result = True
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if x.get("type") == "text")
                    texts.append("[tool result: %s]\n%s" % (tool_names.get(b.get("tool_use_id"), "tool"), truncate(c or "")))
                # thinking/image blocks are skipped (signatures don't transfer)
        if not texts:
            continue
        text = "\n\n".join(texts)
        # tool results belong to the assistant's turn, not a new human turn
        if is_tool_result or role == "assistant":
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["text"] += "\n\n" + text
                turns[-1]["src_ids"].append(e["uuid"])
                continue
            role = "assistant"
        elif turns and turns[-1]["role"] == "user":
            turns[-1]["text"] += "\n\n" + text
            turns[-1]["src_ids"].append(e["uuid"])
            continue
        turns.append({"role": role, "text": text, "ts": ts, "src_ids": [e["uuid"]]})
    return turns


def cc_sessions(directory=None):
    res = []
    if not os.path.isdir(CC_PROJECTS):
        return res
    dirs = [cc_slug(directory)] if directory else os.listdir(CC_PROJECTS)
    for d in dirs:
        pdir = os.path.join(CC_PROJECTS, d)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            title, cwd, first_user, n = None, None, None, 0
            try:
                for e in cc_read_entries(path):
                    t = e.get("type")
                    if t in ("ai-title", "custom-title", "summary"):
                        title = e.get("customTitle") or e.get("aiTitle") or e.get("summary") or title
                    elif t in ("user", "assistant") and not e.get("isSidechain"):
                        n += 1
                        cwd = cwd or e.get("cwd")
                        if t == "user" and first_user is None and not e.get("isMeta"):
                            c = (e.get("message") or {}).get("content")
                            if isinstance(c, str):
                                c = clean_user_text(c)
                                if c:
                                    first_user = c
            except Exception:
                continue
            if n == 0:
                continue
            res.append({
                "tool": "claude", "id": fn[:-6], "path": path,
                "dir": cwd or directory or d, "title": title or (first_user or "(untitled)")[:70],
                "updated": int(os.path.getmtime(path) * 1000), "count": n,
            })
    return res


def cc_session_path(sid):
    for d in os.listdir(CC_PROJECTS):
        p = os.path.join(CC_PROJECTS, d, sid + ".jsonl")
        if os.path.exists(p):
            return p
    return None


def cc_version():
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20).stdout.split()[0]
    except Exception:
        return "2.0.0"


def cc_entry(role, text, ts, sid, cwd, parent, version):
    u = str(uuidlib.uuid4())
    e = {
        "parentUuid": parent, "isSidechain": False, "userType": "external",
        "cwd": cwd, "sessionId": sid, "version": version, "gitBranch": "",
        "type": role, "uuid": u, "timestamp": iso(ts),
    }
    if role == "user":
        e["message"] = {"role": "user", "content": text}
    else:
        e["message"] = {
            "id": "msg_" + uuidlib.uuid4().hex[:24], "type": "message", "role": "assistant",
            "model": "<synthetic>", "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    return e


def cc_last_uuid(path):
    chain = cc_main_chain(cc_read_entries(path))
    return chain[-1]["uuid"] if chain else None


def cc_write_turns(path, turns, sid, cwd, title=None):
    """Append turns to a Claude Code JSONL (create if missing). Returns new uuids per turn."""
    version = cc_version()
    parent = cc_last_uuid(path) if os.path.exists(path) else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_ids = []
    with open(path, "a", encoding="utf-8") as f:
        if title and parent is None:
            f.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}) + "\n")
        for t in turns:
            e = cc_entry(t["role"], t["text"], t["ts"], sid, cwd, parent, version)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            parent = e["uuid"]
            new_ids.append(e["uuid"])
    return new_ids


# ---------------------------------------------------------------- OpenCode side


def oc_conn():
    if not os.path.exists(OC_DB):
        return None
    return sqlite3.connect("file:%s?mode=ro" % OC_DB, uri=True, timeout=10)


def oc_sessions(directory=None):
    con = oc_conn()
    if not con:
        return []
    q = ("select s.id, s.directory, s.title, s.time_updated, "
         "(select count(*) from message m where m.session_id=s.id) "
         "from session s where s.parent_id is null and s.time_archived is null")
    args = []
    if directory:
        q += " and s.directory=?"
        args.append(directory)
    rows = con.execute(q, args).fetchall()
    con.close()
    return [{"tool": "opencode", "id": r[0], "dir": r[1], "title": r[2], "updated": r[3], "count": r[4]}
            for r in rows if r[4] > 0]


def oc_session_exists(sid):
    con = oc_conn()
    if not con:
        return False
    r = con.execute("select 1 from session where id=?", (sid,)).fetchone()
    con.close()
    return bool(r)


def oc_export(sid):
    # opencode truncates stdout at 64KB when writing to a pipe, so write to a file
    fd, path = tempfile.mkstemp(suffix=".json", prefix="aisync-export-")
    try:
        with os.fdopen(fd, "w") as f:
            p = subprocess.run(["opencode", "export", sid], stdout=f, stderr=subprocess.PIPE, text=True)
        with open(path, encoding="utf-8", errors="replace") as f:
            out = f.read()
    finally:
        os.unlink(path)
    i = out.find("{")
    if p.returncode != 0 or i < 0:
        raise RuntimeError("opencode export failed: %s" % (p.stderr.strip() or out[:300]))
    return json.loads(out[i:])


def oc_import(data):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="aisync-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    p = subprocess.run(["opencode", "import", path], capture_output=True, text=True)
    os.unlink(path)
    if p.returncode != 0:
        raise RuntimeError("opencode import failed: %s" % (p.stderr.strip() or p.stdout.strip()))


def oc_turns(sid, data=None):
    data = data or oc_export(sid)
    turns = []
    for m in data.get("messages", []):
        info = m["info"]
        role = info.get("role")
        if role not in ("user", "assistant"):
            continue
        texts = []
        for p in m.get("parts", []):
            pt = p.get("type")
            if pt == "text" and not p.get("synthetic") and p.get("text", "").strip():
                texts.append(p["text"])
            elif pt == "tool":
                st = p.get("state", {})
                s = "[tool call: %s] %s" % (p.get("tool"), truncate(st.get("input", {}), 800))
                if st.get("status") == "completed":
                    s += "\n[tool result]\n" + truncate(st.get("output", ""))
                elif st.get("status") == "error":
                    s += "\n[tool error]\n" + truncate(st.get("error", ""))
                texts.append(s)
            elif pt == "file":
                texts.append("[file: %s]" % (p.get("filename") or p.get("url", "")))
        if not texts:
            continue
        ts = (info.get("time") or {}).get("created") or now_ms()
        if turns and turns[-1]["role"] == role:  # merge multi-step replies into one turn
            turns[-1]["text"] += "\n\n" + "\n\n".join(texts)
            turns[-1]["src_ids"].append(info["id"])
            continue
        turns.append({"role": role, "text": "\n\n".join(texts), "ts": ts, "src_ids": [info["id"]]})
    return turns, data


def oc_build_message(role, text, ts, sid, cwd, parent_id, model):
    mid = oc_id("msg", ms=ts)
    pid = oc_id("prt", ms=ts)
    info = {"id": mid, "sessionID": sid, "role": role, "time": {"created": ts}}
    if role == "user":
        info.update({"agent": "build", "model": {"providerID": model[0], "modelID": model[1]}})
    else:
        info.update({
            "parentID": parent_id, "mode": "build", "agent": "build",
            "path": {"cwd": cwd, "root": cwd}, "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": model[1], "providerID": model[0],
            "time": {"created": ts, "completed": ts}, "finish": "stop",
        })
    part = {"id": pid, "sessionID": sid, "messageID": mid, "type": "text", "text": text}
    return {"info": info, "parts": [part]}


def oc_default_model():
    con = oc_conn()
    if con:
        r = con.execute("select model from session where model is not null order by time_updated desc limit 1").fetchone()
        con.close()
        try:
            m = json.loads(r[0])
            return (m.get("providerID"), m.get("id") or m.get("modelID"))
        except Exception:
            pass
    return ("opencode", "big-pickle")


def oc_append_messages(data, turns):
    """Add turns to an exported OpenCode session dict. Returns new message ids."""
    sid = data["info"]["id"]
    cwd = data["info"]["directory"]
    model = oc_default_model()
    msgs = data.setdefault("messages", [])
    last_user = next((m["info"]["id"] for m in reversed(msgs) if m["info"].get("role") == "user"), None)
    last_ts = max([(m["info"].get("time") or {}).get("created", 0) for m in msgs] + [0])
    new_ids = []
    for t in turns:
        ts = max(t["ts"], last_ts + 1)  # keep ordering monotonic
        last_ts = ts
        if t["role"] == "user":
            m = oc_build_message("user", t["text"], ts, sid, cwd, None, model)
            last_user = m["info"]["id"]
        else:
            if last_user is None:  # OpenCode requires every assistant message to have a parent user message
                u = oc_build_message("user", "(conversation started without a user message)", ts, sid, cwd, None, model)
                msgs.append(u)
                new_ids.append(u["info"]["id"])
                last_user = u["info"]["id"]
            m = oc_build_message("assistant", t["text"], ts, sid, cwd, last_user, model)
        msgs.append(m)
        new_ids.append(m["info"]["id"])
    data["info"]["time"]["updated"] = max(data["info"]["time"].get("updated", 0), last_ts)
    return new_ids


def oc_new_session(title, cwd, ts):
    sid = oc_id("ses", descending=True, ms=now_ms())
    return {
        "info": {
            "id": sid, "slug": "aisync-" + sid[-6:].lower(), "projectID": "global",
            "directory": cwd, "title": title, "version": "aisync",
            "summary": {"additions": 0, "deletions": 0, "files": 0},
            "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "time": {"created": ts, "updated": ts},
        },
        "messages": [],
    }


def oc_replace(data):
    """Write an updated session back into OpenCode (import upserts by id; fall back to delete+import)."""
    sid = data["info"]["id"]
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if oc_session_exists(sid):
        before = oc_export(sid)
        with open(os.path.join(BACKUP_DIR, "%s-%d.json" % (sid, now_ms())), "w") as f:
            json.dump(before, f)
        oc_import(data)
        after_ids = {m["info"]["id"] for m in oc_export(sid).get("messages", [])}
        want = {m["info"]["id"] for m in data["messages"]}
        if want <= after_ids:
            return
        # import did not upsert new messages: delete + reimport full copy
        subprocess.run(["opencode", "session", "delete", sid], capture_output=True, text=True)
    oc_import(data)


# ---------------------------------------------------------------- sync logic


def all_sessions(directory):
    return sorted(cc_sessions(directory) + oc_sessions(directory), key=lambda s: s["updated"], reverse=True)


def sync_oc_to_cc(state, oc_sid, verbose=True):
    """Make sure every OpenCode message of oc_sid exists in its paired Claude session."""
    pair = find_pair(state, oc=oc_sid)
    turns, data = oc_turns(oc_sid)
    info = data["info"]
    if pair is None:
        cc_sid = str(uuidlib.uuid4())
        pair = {"oc": oc_sid, "cc": cc_sid, "dir": info["directory"], "oc_known": [], "cc_known": []}
        state["pairs"].append(pair)
        path = os.path.join(CC_PROJECTS, cc_slug(info["directory"]), cc_sid + ".jsonl")
        title = "%s [from opencode]" % info.get("title", "")
    else:
        path = cc_session_path(pair["cc"]) or os.path.join(CC_PROJECTS, cc_slug(pair["dir"]), pair["cc"] + ".jsonl")
        title = None
    known = set(pair["oc_known"])
    new = [t for t in turns if not set(t["src_ids"]) & known]
    if new:
        new = [dict(t, text="[from opencode] " + t["text"]) for t in new]
        ids = cc_write_turns(path, new, pair["cc"], pair["dir"], title)
        pair["cc_known"] += ids
        for t in new:
            pair["oc_known"] += t["src_ids"]
        if verbose:
            print("  opencode %s -> claude %s : +%d turns" % (oc_sid, pair["cc"], len(new)))
    pair["synced_at"] = now_ms()
    return pair


def sync_cc_to_oc(state, cc_sid, verbose=True):
    """Make sure every Claude message of cc_sid exists in its paired OpenCode session."""
    path = cc_session_path(cc_sid)
    if not path:
        raise RuntimeError("claude session %s not found" % cc_sid)
    turns = cc_turns(path)
    pair = find_pair(state, cc=cc_sid)
    if pair is None or not oc_session_exists(pair["oc"]):
        cwd = next((e.get("cwd") for e in cc_read_entries(path) if e.get("cwd")), HOME)
        title = next((s["title"] for s in cc_sessions(cwd) if s["id"] == cc_sid), "claude session")
        data = oc_new_session("%s [from claude]" % title, cwd, turns[0]["ts"] if turns else now_ms())
        if pair is None:
            pair = {"oc": data["info"]["id"], "cc": cc_sid, "dir": cwd, "oc_known": [], "cc_known": []}
            state["pairs"].append(pair)
        else:
            pair.update({"oc": data["info"]["id"], "cc_known": [], "oc_known": []})
    else:
        data = oc_export(pair["oc"])
    known = set(pair["cc_known"])
    new = [t for t in turns if not set(t["src_ids"]) & known]
    if new:
        new = [dict(t, text="[from claude] " + t["text"]) for t in new]
        ids = oc_append_messages(data, new)
        oc_replace(data)
        pair["oc_known"] += ids
        for t in new:
            pair["cc_known"] += t["src_ids"]
        if verbose:
            print("  claude %s -> opencode %s : +%d turns" % (cc_sid, pair["oc"], len(new)))
    pair["synced_at"] = now_ms()
    return pair


def sync_pair_both(state, pair):
    # order matters: pull OpenCode into Claude first, then push Claude (incl. new) back
    if oc_session_exists(pair["oc"]):
        sync_oc_to_cc(state, pair["oc"])
    if cc_session_path(pair["cc"]):
        sync_cc_to_oc(state, pair["cc"])


def warn_running():
    msgs = []
    if running("opencode"):
        msgs.append("opencode")
    if msgs:
        print("! note: %s is running. Messages written while a session is open may not show until you reopen it." % ", ".join(msgs), file=sys.stderr)


def resolve(sid):
    """Return ('claude'|'opencode', full id) from a full or prefix id."""
    if sid.startswith("ses_"):
        return "opencode", sid
    if cc_session_path(sid):
        return "claude", sid
    cands = [s for s in all_sessions(None) if s["id"].startswith(sid)]
    if len(cands) == 1:
        return cands[0]["tool"], cands[0]["id"]
    if not cands:
        raise SystemExit("no session matches %r" % sid)
    raise SystemExit("ambiguous id %r: %s" % (sid, ", ".join(c["id"] for c in cands[:5])))


# ---------------------------------------------------------------- commands


def cmd_list(a):
    directory = None if a.all else os.path.abspath(a.dir)
    state = load_state()
    rows = all_sessions(directory)[: a.limit]
    if not rows:
        print("no sessions for %s (use --all)" % (directory or "any dir"))
        return
    for i, s in enumerate(rows, 1):
        pair = find_pair(state, oc=s["id"]) if s["tool"] == "opencode" else find_pair(state, cc=s["id"])
        link = ""
        if pair:
            link = " <-> %s" % (pair["cc"][:8] if s["tool"] == "opencode" else pair["oc"][:14])
        where = "" if directory else "  " + s["dir"].replace(HOME, "~")
        print("%3d  %-8s  %s  %-36s %4d msg  %s%s%s" % (
            i, s["tool"], fmt_time(s["updated"]), s["id"], s["count"], s["title"][:60], link, where))
    return rows


def cmd_show(a):
    tool, sid = resolve(a.id)
    turns = cc_turns(cc_session_path(sid)) if tool == "claude" else oc_turns(sid)[0]
    for t in turns[-a.last:] if a.last else turns:
        print("\n=== %s  (%s) ===" % (t["role"].upper(), fmt_time(t["ts"])))
        print(t["text"])


def cmd_sync(a):
    warn_running()
    state = load_state()
    if a.id:
        tool, sid = resolve(a.id)
        pair = find_pair(state, oc=sid) if tool == "opencode" else find_pair(state, cc=sid)
        if pair:
            sync_pair_both(state, pair)
        elif tool == "opencode":
            sync_oc_to_cc(state, sid)
        else:
            sync_cc_to_oc(state, sid)
        save_state(state)
        return
    directory = None if a.all else os.path.abspath(a.dir)
    sessions = all_sessions(directory)
    if a.since:
        cutoff = now_ms() - a.since * 86400000
        sessions = [s for s in sessions if s["updated"] >= cutoff]
    print("syncing %d sessions in %s" % (len(sessions), directory or "all dirs"))
    for s in sessions:
        try:
            if s["tool"] == "opencode":
                pair = find_pair(state, oc=s["id"])
                if pair:
                    sync_pair_both(state, pair)
                else:
                    sync_oc_to_cc(state, s["id"])
            else:
                pair = find_pair(state, cc=s["id"])
                if pair:
                    sync_pair_both(state, pair)
                else:
                    sync_cc_to_oc(state, s["id"])
            save_state(state)
        except Exception as e:
            print("  ! %s %s: %s" % (s["tool"], s["id"], e), file=sys.stderr)
    print("done")


def cmd_resume(a):
    tool, sid = resolve(a.id)
    state = load_state()
    target = a.target
    pair = find_pair(state, oc=sid) if tool == "opencode" else find_pair(state, cc=sid)
    if pair:
        sync_pair_both(state, pair)
    elif tool == "opencode":
        pair = sync_oc_to_cc(state, sid)
    else:
        pair = sync_cc_to_oc(state, sid)
    save_state(state)
    os.chdir(pair["dir"] if os.path.isdir(pair["dir"]) else HOME)
    if target == "claude":
        cmd = ["claude", "--resume", pair["cc"]]
    else:
        cmd = ["opencode", "--session", pair["oc"]]
    print("$ (cd %s && %s)" % (os.getcwd(), " ".join(cmd)))
    os.execvp(cmd[0], cmd)


def cmd_pick(a):
    rows = cmd_list(a)
    if not rows:
        return
    try:
        choice = input("\nnumber to open (enter = quit): ").strip()
        if not choice:
            return
        s = rows[int(choice) - 1]
        where = input("continue in [c]laude or [o]pencode? ").strip().lower()
    except (EOFError, KeyboardInterrupt, ValueError, IndexError):
        return
    a.id = s["id"]
    a.target = "opencode" if where.startswith("o") else "claude"
    cmd_resume(a)


def cmd_context(a):
    """Print a compact markdown handoff to paste into any AI tool."""
    tool, sid = resolve(a.id)
    turns = cc_turns(cc_session_path(sid)) if tool == "claude" else oc_turns(sid)[0]
    print("# Handoff from %s session %s\n" % (tool, sid))
    print("Continue this conversation. Earlier turns (tool output truncated):\n")
    for t in turns[-a.last:]:
        print("## %s\n%s\n" % (t["role"], truncate(t["text"], 3000)))


def main():
    ap = argparse.ArgumentParser(prog="aisync", description="See and sync Claude Code <-> OpenCode conversations.")
    sub = ap.add_subparsers(dest="cmd")

    def scope(p):
        p.add_argument("--dir", default=".", help="project directory (default: cwd)")
        p.add_argument("--all", action="store_true", help="every directory")
        p.add_argument("-n", "--limit", type=int, default=30)

    p = sub.add_parser("list", help="list sessions from both tools"); scope(p); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("pick", help="interactive: choose a session and where to continue it"); scope(p); p.set_defaults(fn=cmd_pick)
    p = sub.add_parser("show", help="print a session transcript"); p.add_argument("id"); p.add_argument("--last", type=int, default=0); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("sync", help="two-way sync (one id, or all sessions in dir)")
    p.add_argument("id", nargs="?"); p.add_argument("--dir", default="."); p.add_argument("--all", action="store_true")
    p.add_argument("--since", type=int, default=0, help="only sessions updated in last N days")
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("resume", help="sync one session then open it in claude or opencode")
    p.add_argument("id"); p.add_argument("target", choices=["claude", "opencode"]); p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("context", help="print markdown handoff for copy/paste"); p.add_argument("id"); p.add_argument("--last", type=int, default=20); p.set_defaults(fn=cmd_context)

    a = ap.parse_args()
    if not a.cmd:
        a = ap.parse_args(["pick"])
    a.fn(a)


if __name__ == "__main__":
    main()
