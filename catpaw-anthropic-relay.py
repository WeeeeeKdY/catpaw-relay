#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CatPaw Anthropic Relay
======================
把 Claude Code / pi 的 Anthropic Messages API (`/v1/messages`) 请求，
转成 CatPaw 后端协议。

双通道（协议移植自 pawpass，2026-08 逆向验证）：

  1. 纯文本轮次  -> gateway 通道
     https://catpaw.sankuai.com/api/gpt/openai/stream
     （IDE 面板端点，userModelTypeCode 路由，官方额度计量）

  2. 含工具 / tool_result 轮次 -> agent 通道
     https://catpaw-agent.sankuai.com
     （FRIDAY agent 中继）
       ① POST /api/oauth/token-exchange   SSO -> agent access_token (~25min)
       ② POST /api/agent/conversation/round   开新轮
       ③ POST /api/agent/conversation/event   会话状态
       ④ POST /api/agent/conversation/turn    SSE 流式推理
     toolConfigs 携带 fromClient:true 注册客户端工具；模型通过
     SSE tool_use 块发起原生函数调用，客户端本地执行后以
     message.type="tool" 回传 tool_result。

用法:
    python3 catpaw-anthropic-relay.py [--port 8789] [--host 127.0.0.1]

然后在 Claude Code 里:
    ANTHROPIC_BASE_URL=http://127.0.0.1:8789 claude --model gpt-6-sol
"""

import hashlib
import json
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8789
UPSTREAM = "https://catpaw.sankuai.com"
AGENT_BASE = "https://catpaw-agent.sankuai.com"

SSO_CONFIG = os.path.expanduser("~/.catpaw/sso_config.json")
VSCODE_DB = os.path.expanduser(
    "~/Library/Application Support/CatPaw IDE/User/globalStorage/state.vscdb"
)

# model 名(小写, 去 [1M] 等后缀) -> catpaw modelType
MODEL_MAP = {
    "gpt-6-sol": 101,
    "gpt-6-luna": 100,
    "gpt-6-astra": 95,
    "gpt-6.1-sol": 102,
    "gpt-6.1-luna": 103,
    "gpt-5.6-sol": 82,
    "gpt-5.6-luna": 81,
    "gpt-5.6-terra": 80,
    "deepseek-v4-pro": 64,
    "deepseek-v4-flash": 63,
    "deepseek-v4.1-flash": 97,
    "kimi-k3": 83,
    "kimi-k2.7-code": 78,
    "longcat-2.0": 77,
    "glm-5.3": 89,
    "glm-5.3-flash": 91,
    "glm-5.3-flash-advisor": 96,
    "glm-5.3-flashx": 98,
    "glm-5.2": 75,
    "glm-5v-turbo": 60,
    "minimax-m3": 70,
    "grok-4.6": 92,
    "gemini-3.6-flash": 84,
    "gemini-3.7-flash": 90,
    "gemini-3.8-flash": 94,
    "claude-opus-5.5": 99,
    "hy4-preview": 93,
}

DEFAULT_MODEL = "gpt-6-sol"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_lock = threading.Lock()


def log(*args):
    print("[relay]", *args, flush=True)


def get_token():
    """动态读取 CatPaw IDE 的 SSO token 和 misId。"""
    # 1. ~/.catpaw/sso_config.json（Agent SSO，CatPaw IDE 登录后会自动刷新）
    try:
        with open(SSO_CONFIG, "r", encoding="utf-8") as f:
            d = json.load(f)
        t = d.get("ssoid")
        if t:
            return t, d.get("misId") or os.getenv("CATPAW_MIS_ID", "")
    except Exception:
        pass
    # 2. CatPaw IDE 的 state.vscdb
    try:
        con = sqlite3.connect(VSCODE_DB)
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key='mt-idekit.mt-idekit-code'"
        ).fetchone()
        con.close()
        if row:
            d = json.loads(row[0])
            t = d.get("accessTokenprod")
            u = (d.get("userInfoprod") or {}).get("misId") or os.getenv("CATPAW_MIS_ID", "")
            if t:
                return t, u
    except Exception:
        pass
    return None, os.getenv("CATPAW_MIS_ID", "")


def normalize_model(name):
    n = (name or "").strip().lower()
    for suffix in ("[1m]", "[500k]", "[200k]", "[64k]"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def model_to_code(name):
    return MODEL_MAP.get(normalize_model(name))


def extract_text(content):
    """把 Anthropic 的 content（字符串或 blocks 数组）转成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                parts.append(
                    "[tool_use %s: %s]"
                    % (block.get("name"), json.dumps(block.get("input", {}), ensure_ascii=False))
                )
            elif t == "tool_result":
                parts.append("[tool_result] " + extract_text(block.get("content")))
            elif t == "image":
                parts.append("[image]")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def extract_system(system):
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(system)


def _as_blocks(content):
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": extract_text(content)}]


def _parse_tool_args(raw):
    """把累积的 arguments/toolParams 字符串解析成对象；失败则返回 {}。"""
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


def _has_tool_result(messages):
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        for blk in _as_blocks(m.get("content")):
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                return True
    return False


# ======================================================================
# gateway 通道（纯文本）
# ======================================================================

def build_gateway_messages(anthropic):
    """纯文本轮次：只取 system/user/assistant 文本。"""
    msgs = []
    sys_text = extract_system(anthropic.get("system"))
    if sys_text:
        msgs.append({"role": "system", "content": sys_text})
    for m in anthropic.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        txt = extract_text(m.get("content"))
        if txt:
            msgs.append({"role": role, "content": txt})
    if not msgs:
        msgs.append({"role": "user", "content": "Hello"})
    return msgs


def call_gateway_stream(msgs, code, token, misid):
    body = {
        "triggerMode": "AGENT",
        "userModelTypeCode": code,
        "userModelParams": {"context": "204800", "thinking": "true", "effort": "high"},
        "messages": msgs,
        "conversationId": "relay-" + uuid.uuid4().hex[:16],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cookie": "1d47d6ff96_ssoid=%s; f32a546874_ssoid=%s" % (token, token),
        "ide-type": "CatPaw IDE",
        "client-type": "CatPaw IDE",
        "user-mis-id": misid,
        "user-uid": misid,
        "mis-id": misid,
        "tenant": "4391f0be98",
        "Catpaw-Auth": token,
    }
    req = urllib.request.Request(
        UPSTREAM + "/api/gpt/openai/stream",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=600, context=_ssl_ctx)


# ======================================================================
# agent 通道（工具闭环）
# ======================================================================

_agent_token_cache = {}


def fetch_agent_token(token, misid):
    """SSO -> agent access_token（~25min），本地缓存 24min。"""
    with _lock:
        cached = _agent_token_cache.get(token)
        if cached and cached[1] > time.time():
            return cached[0]
    req = urllib.request.Request(
        AGENT_BASE + "/api/oauth/token-exchange",
        data=json.dumps({"accessToken": token, "audiences": ["60921859"]}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "access-token": token,
            "Cookie": "1d47d6ff96_ssoid=%s" % token,
            "mis-id": misid,
            "user-mis-id": misid,
            "user-uid": misid,
        },
    )
    resp = urllib.request.urlopen(req, timeout=60, context=_ssl_ctx)
    data = json.loads(resp.read().decode("utf-8"))
    at = (data.get("data") or {}).get("access_token")
    if not at:
        raise RuntimeError("token-exchange 响应缺少 access_token（SSO 可能过期）")
    with _lock:
        _agent_token_cache[token] = (at, time.time() + 24 * 60)
    return at


def _agent_headers(at, misid, accept="application/json"):
    cookie = "1d47d6ff96_ssoid=%s" % at
    return {
        "Content-Type": "application/json",
        "access-token": at,
        "Cookie": cookie,
        "Catpaw-Cookie": cookie,
        "Accept": accept,
        "mis-id": misid,
        "user-mis-id": misid,
        "user-uid": misid,
    }


def _is_zero(v):
    return v in (None, 0, "0")


def agent_post(at, misid, path, body):
    """agent 通道普通 POST（round/event），返回解析后的 JSON dict。"""
    req = urllib.request.Request(
        AGENT_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers=_agent_headers(at, misid),
    )
    resp = urllib.request.urlopen(req, timeout=300, context=_ssl_ctx)
    data = json.loads(resp.read().decode("utf-8"))
    if data.get("success") is False or not _is_zero(data.get("code")) or not _is_zero(data.get("unifyCode")):
        raise RuntimeError("agent %s 失败: %s" % (path, data.get("msg") or data.get("message") or data))
    return data


class AgentBusyError(RuntimeError):
    """上游会话忙，短退避重试后仍未恢复。"""


def agent_post_retry(at, misid, path, body):
    """agent 普通 POST，带“会话忙”退避重试（0/2/4 秒）。

    上游在会话还在执行上一轮时会拒绝创建新轮次（“会话正在执行中，
    无法创建新轮次”）。这里像 turn 一样短退避重试，给上一轮几秒收尾。
    """
    last_err = None
    for wait in (0, 2, 4):
        if wait:
            time.sleep(wait)
        try:
            return agent_post(at, misid, path, body)
        except RuntimeError as e:
            last_err = e
            if _is_busy_error(str(e)):
                continue
            raise
    raise AgentBusyError("agent %s 失败: %s" % (path, last_err))


def agent_post_sse(at, misid, path, body):
    """agent 通道 SSE POST（turn），返回原始 response。"""
    req = urllib.request.Request(
        AGENT_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers=_agent_headers(at, misid, accept="text/event-stream"),
    )
    return urllib.request.urlopen(req, timeout=600, context=_ssl_ctx)


def build_tool_configs(anthropic):
    """Anthropic tools -> catpaw agent 通道 toolConfigs。

    工具名保持 Anthropic 原名（Bash / Read / Write...），
    fromClient:true 表示由客户端本地执行、结果以 tool_result 回传。
    """
    configs = []
    for tool in anthropic.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        configs.append(
            {
                "name": tool["name"],
                "enable": True,
                "description": tool.get("description") or "",
                "inputSchema": schema,
                "fromClient": True,
            }
        )
    return configs


def scan_history(anthropic):
    """把回放消息拍平成条目序列。条目 = (kind, ...):
    ("user", text) / ("asst", text) / ("fc", callId, name, args) / ("out", callId, name, output)
    """
    items = []
    for m in anthropic.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            text_parts = []
            outs = []
            for blk in _as_blocks(content):
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    text_parts.append(blk.get("text", ""))
                elif t == "tool_result":
                    outs.append(blk)
            if text_parts:
                items.append(("user", "\n".join(text_parts)))
            for tr in outs:
                items.append(
                    ("out", tr.get("tool_use_id") or "", "", extract_text(tr.get("content")))
                )
        elif role == "assistant":
            for blk in _as_blocks(content):
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text" and blk.get("text"):
                    items.append(("asst", blk["text"]))
                elif t == "tool_use":
                    items.append(
                        ("fc", blk.get("id") or "", blk.get("name") or "", blk.get("input") or {})
                    )
    return items


def _short_hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _item_sig(item):
    kind = item[0]
    if kind in ("user", "asst"):
        return _short_hash("%s:%s" % (kind, item[1]))
    if kind == "fc":
        return _short_hash(
            "fc:%s:%s:%s" % (item[1], item[2], json.dumps(item[3], sort_keys=True, ensure_ascii=False))
        )
    return _short_hash("out:%s:%s" % (item[1], item[3]))


def build_snapshot(items):
    """新会话导入全量历史时压缩成一条文本快照。"""
    parts = []
    for it in items:
        if it[0] == "user":
            parts.append("[用户]\n%s" % it[1])
        elif it[0] == "asst":
            parts.append("[助手]\n%s" % it[1])
        elif it[0] == "fc":
            parts.append("[助手调用工具 %s，callId=%s]\n%s" % (it[2], it[1], json.dumps(it[3], ensure_ascii=False)))
        elif it[0] == "out":
            parts.append("[工具 %s 返回，callId=%s]\n%s" % (it[2], it[1], it[3]))
    return "以下是客户端保存的会话上下文快照。请延续该上下文，不要复述快照，直接处理最后的待办。\n\n" + "\n\n".join(parts)


_sessions = {}
SESSION_MAX = 32
CONVERSATION_TTL = 4 * 3600


def _new_session(key):
    """创建并登记一个新会话，返回 sess dict。"""
    if len(_sessions) >= SESSION_MAX:
        oldest = min(_sessions, key=lambda k: _sessions[k]["updated"])
        del _sessions[oldest]
    sess = {
        "cid": uuid.uuid4().hex,
        "consumed": 0,
        "sig": "",
        "callnames": {},
        "updated": time.time(),
        "_key": key,
    }
    _sessions[key] = sess
    return sess


def _turn_body(cid, msg, code, tool_configs, system_text):
    b = {
        "conversationId": cid,
        "message": msg,
        "modelType": code,
        "source": "CatPawCLI",
        "mode": "CLI",
        "toolVersion": "2.0.0",
        "toolConfigs": tool_configs,
    }
    if system_text:
        b["rulesMessage"] = system_text
    return b


def drain_turn(at, misid, body):
    """读完整 turn SSE 但不吐 UI（中间步骤回放）。"""
    resp = agent_post_sse(at, misid, "/api/agent/conversation/turn", body)
    try:
        for ev in iter_agent_events(resp):
            if (ev.get("message") or {}).get("finished"):
                return
    finally:
        try:
            resp.close()
        except Exception:
            pass


def iter_agent_events(resp):
    """逐帧解析 agent turn SSE。"""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if ev.get("error"):
            raise RuntimeError("agent turn: %s" % ev["error"])
        yield ev


def _is_busy_error(msg):
    """判断是否为上游会话并发忙碌错误。

    覆盖两种：turn 阶段的“会话正在被处理”（1004010009），以及
    round 阶段的“会话正在执行中，无法创建新轮次”。
    """
    return (
        "正在被处理" in msg
        or "1004010009" in msg
        or "正在执行" in msg
        or "无法创建新轮次" in msg
    )


def _peek_agent_event(resp):
    """读取 agent turn SSE 的第一个事件；流为空返回 None。

    如果首个事件是 error（例如“会话正在被处理”），会抛 RuntimeError。
    """
    for ev in iter_agent_events(resp):
        return ev
    return None


def agent_stream(anthropic, code, token, misid, tool_configs):
    """agent 通道主流程。

    返回 (sse_response, sess, items, full_sig, first_ev)。first_ev 为
    已预读的首个 turn SSE 事件（用于尽早发现“会话正在被处理”并退避重试）。
    调用方读完流后需调用 _finish_agent_session(sess, items, full_sig) 推进会话游标。
    """
    system_text = extract_system(anthropic.get("system"))
    items = scan_history(anthropic)
    first_user = next((it[1] for it in items if it[0] == "user"), "")
    key = hashlib.sha256(
        json.dumps([code, system_text, first_user], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    full_sig = "".join(_item_sig(it) for it in items)
    now = time.time()

    at = fetch_agent_token(token, misid)

    with _lock:
        sess = _sessions.get(key)
        if sess and (
            not full_sig.startswith(sess["sig"])
            or now - sess["updated"] > CONVERSATION_TTL
            or (sess["consumed"] == len(items) and sess["sig"] == full_sig)
        ):
            del _sessions[key]
            sess = None
        if not sess:
            sess = _new_session(key)

    def tb(cid_, msg):
        return _turn_body(cid_, msg, code, tool_configs, system_text)

    def _run(sess):
        cid = sess["cid"]
        callnames = sess["callnames"]

        # ---- 增量分组 ----
        groups = []
        if sess["consumed"] == 0 and len(items) > 1:
            groups.append(
                (
                    "user",
                    {
                        "type": "user",
                        "messageId": str(uuid.uuid4()),
                        "content": [{"type": "text", "text": build_snapshot(items)}],
                    },
                )
            )
        else:
            pending = items[sess["consumed"]:]
            out_buf = []

            def flush_tool():
                if not out_buf:
                    return
                blocks = []
                for o in out_buf:
                    cid_ = o[1]
                    if cid_ in callnames:
                        blocks.append(
                            {
                                "type": "tool_result",
                                "toolCallId": cid_,
                                "toolName": callnames[cid_],
                                "toolResult": o[3],
                            }
                        )
                if blocks:
                    groups.append(
                        ("tool", {"type": "tool", "messageId": str(uuid.uuid4()), "content": blocks})
                    )
                out_buf.clear()

            for it in pending:
                if it[0] == "user":
                    flush_tool()
                    groups.append(
                        (
                            "user",
                            {
                                "type": "user",
                                "messageId": str(uuid.uuid4()),
                                "content": [{"type": "text", "text": it[1]}],
                            },
                        )
                    )
                elif it[0] == "fc":
                    if it[1]:
                        callnames[it[1]] = it[2]
                elif it[0] == "out":
                    out_buf.append(it)
            flush_tool()

        if not groups:
            groups.append(
                (
                    "user",
                    {
                        "type": "user",
                        "messageId": str(uuid.uuid4()),
                        "content": [{"type": "text", "text": "继续。"}],
                    },
                )
            )

        # ---- 依次发分组 ----
        saw_round = sess["consumed"] > 0
        for gi, (kind, data) in enumerate(groups):
            last = gi == len(groups) - 1
            if kind == "user":
                if saw_round:
                    agent_post_retry(
                        at,
                        misid,
                        "/api/agent/conversation/event",
                        {
                            "conversationId": cid,
                            "eventType": "conversation",
                            "data": {"status": "completed"},
                        },
                    )
                agent_post_retry(at, misid, "/api/agent/conversation/round", tb(cid, data))
                agent_post_retry(
                    at,
                    misid,
                    "/api/agent/conversation/event",
                    {"conversationId": cid, "eventType": "conversation", "data": {"status": "running"}},
                )
                saw_round = True
            if not last:
                drain_turn(at, misid, tb(cid, data))

        # ---- 最后一组 turn（会话忙短退避）----
        final_body = tb(cid, groups[-1][1])
        resp = None
        first_ev = None
        last_err = None
        for wait in (0, 2, 4):
            if wait:
                time.sleep(wait)
            try:
                resp = agent_post_sse(at, misid, "/api/agent/conversation/turn", final_body)
                # 提前读首个 SSE 事件：上游把“会话正在被处理”放在 SSE error 帧里，
                # 而不是 HTTP 错误码，必须在这里发现才能进入退避重试。
                first_ev = _peek_agent_event(resp)
                break
            except urllib.error.HTTPError as e:
                last_err = e
                try:
                    detail = e.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                if _is_busy_error(detail) or "处理" in detail or e.code in (409, 429):
                    continue
                raise
            except RuntimeError as e:
                last_err = e
                if _is_busy_error(str(e)):
                    try:
                        resp.close()
                    except Exception:
                        pass
                    resp = None
                    continue
                try:
                    resp.close()
                except Exception:
                    pass
                resp = None
                raise
        if resp is None:
            raise AgentBusyError("agent turn 失败: %s" % last_err)

        return resp, sess, items, full_sig, first_ev

    # 会话忙时的自愈：先丢弃当前会话，换一个全新 conversationId 走快照导入。
    for attempt in (0, 1):
        try:
            return _run(sess)
        except AgentBusyError:
            if attempt == 1:
                raise
            _drop_agent_session(sess)
            with _lock:
                sess = _new_session(key)
            continue
        except Exception:
            _drop_agent_session(sess)
            raise


def _finish_agent_session(sess, items, full_sig):
    with _lock:
        sess["consumed"] = len(items)
        sess["sig"] = full_sig
        sess["updated"] = time.time()


def _drop_agent_session(sess):
    with _lock:
        _sessions.pop(sess.get("_key"), None)


# ======================================================================
# OpenAI 风格 tool_calls 累积器（gateway 通道用）
# ======================================================================

class ToolCallAccumulator:
    """把 catpaw gateway 流式 tool_calls delta 累积成结构化工具调用。"""

    def __init__(self):
        self.calls = {}

    def add(self, tcs):
        events = []
        if not tcs:
            return events
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            oi = tc.get("index", 0)
            cur = self.calls.setdefault(oi, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                cur["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["name"] = fn["name"]
            arg_delta = fn.get("arguments") or ""
            if not arg_delta:
                continue
            if cur["args"] and arg_delta.lstrip().startswith("{"):
                try:
                    json.loads(cur["args"])
                    cur["args"] = arg_delta
                    events.append((oi, arg_delta))
                    continue
                except Exception:
                    pass
            cur["args"] += arg_delta
            events.append((oi, arg_delta))
        return events

    def sorted_items(self):
        return sorted(self.calls.items(), key=lambda kv: kv[0])

    def has_calls(self):
        return len(self.calls) > 0


# ======================================================================
# HTTP handler
# ======================================================================

class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.command, self.path))

    # ---------- helpers ----------

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _anthropic_error(self, status, etype, message):
        self._send_json(
            status,
            {"type": "error", "error": {"type": etype, "message": message}},
        )

    def _sse(self, event, data):
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, payload)).encode("utf-8"))
        self.wfile.flush()

    # ---------- endpoints ----------

    def _path(self):
        return urlparse(self.path).path

    def do_HEAD(self):
        p = self._path()
        if p in ("/v1/models", "/v1/models/", "/api/hello", "/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def do_GET(self):
        p = self._path()
        if p in ("/v1/models", "/v1/models/"):
            data = [
                {
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "catpaw",
                    "type": "model",
                }
                for name in MODEL_MAP.keys()
            ]
            self._send_json(
                200,
                {
                    "data": data,
                    "has_more": False,
                    "first_id": data[0]["id"],
                    "last_id": data[-1]["id"],
                },
            )
        elif p in ("/health", "/", "/api/hello"):
            token, _ = get_token()
            self._send_json(200, {"ok": True, "token_loaded": bool(token)})
        else:
            self._send_json(
                404,
                {"type": "error", "error": {"type": "not_found_error", "message": "not found"}},
            )

    def do_POST(self):
        if self._path() not in ("/v1/messages", "/v1/messages/"):
            self._send_json(
                404,
                {"type": "error", "error": {"type": "not_found_error", "message": "not found"}},
            )
            return
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception as e:
            self._anthropic_error(400, "invalid_request_error", "bad json: %s" % e)
            return

        token, misid = get_token()
        if not token:
            self._anthropic_error(
                401,
                "authentication_error",
                "CatPaw SSO token 不存在。请先登录 CatPaw IDE 刷新 token（~/.catpaw/sso_config.json）。",
            )
            return

        req_model = body.get("model") or DEFAULT_MODEL
        code = model_to_code(req_model)
        if code is None:
            self._anthropic_error(
                400,
                "invalid_request_error",
                "未知模型: %s。可用模型见 GET /v1/models" % req_model,
            )
            return

        stream = bool(body.get("stream"))
        tool_configs = build_tool_configs(body)
        use_agent = bool(tool_configs) or _has_tool_result(body.get("messages"))

        if use_agent:
            log(
                "请求: model=%s tools=%d msgs=%d (agent 通道)"
                % (req_model, len(tool_configs), len(body.get("messages") or []))
            )
            try:
                catpaw_resp, sess, items, full_sig, first_ev = agent_stream(
                    body, code, token, misid, tool_configs
                )
            except urllib.error.HTTPError as e:
                raw = b""
                try:
                    raw = e.read()
                except Exception:
                    pass
                self._anthropic_error(
                    e.code or 502,
                    "api_error",
                    "upstream agent HTTP %s: %s" % (e.code, raw[:300].decode("utf-8", "replace")),
                )
                return
            except Exception as e:
                self._anthropic_error(502, "api_error", "upstream agent error: %s" % e)
                return
            if stream:
                self._handle_agent_stream(catpaw_resp, req_model, sess, items, full_sig, first_ev)
            else:
                self._handle_agent_non_stream(catpaw_resp, req_model, sess, items, full_sig, first_ev)
        else:
            msgs = build_gateway_messages(body)
            log("请求: model=%s msgs=%d (gateway 通道)" % (req_model, len(msgs)))
            try:
                catpaw_resp = call_gateway_stream(msgs, code, token, misid)
            except urllib.error.HTTPError as e:
                raw = b""
                try:
                    raw = e.read()
                except Exception:
                    pass
                self._anthropic_error(
                    e.code or 502,
                    "api_error",
                    "upstream gateway HTTP %s: %s" % (e.code, raw[:300].decode("utf-8", "replace")),
                )
                return
            except Exception as e:
                self._anthropic_error(502, "api_error", "upstream gateway error: %s" % e)
                return
            if stream:
                self._handle_stream(catpaw_resp, req_model)
            else:
                self._handle_non_stream(catpaw_resp, req_model)

    # ---------- gateway 响应转换（纯文本） ----------

    def _handle_non_stream(self, catpaw_resp, req_model):
        text_parts = []
        acc = ToolCallAccumulator()
        usage_in = usage_out = 0
        actual_model = req_model
        while True:
            raw = catpaw_resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                d = json.loads(payload)
            except Exception:
                continue
            if d.get("content") == "[DONE]":
                break
            sc = d.get("statusCode")
            if sc not in (None, 0, "0"):
                msg = d.get("msg") or d.get("message") or d.get("content") or "catpaw error"
                self._anthropic_error(502, "api_error", "upstream statusCode=%s: %s" % (sc, msg))
                return
            if "model" in d and d.get("model"):
                actual_model = d["model"]
            if "usage" in d:
                u = d["usage"]
                usage_in = u.get("prompt_tokens", usage_in)
                usage_out = u.get("completion_tokens", usage_out)
            choices = d.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            content = delta.get("content")
            if content:
                text_parts.append(content)
            acc.add(delta.get("tool_calls"))

        content = []
        text = "".join(text_parts)
        if text:
            content.append({"type": "text", "text": text})
        for _oi, cur in acc.sorted_items():
            content.append(
                {
                    "type": "tool_use",
                    "id": cur["id"] or ("toolu_" + uuid.uuid4().hex[:10]),
                    "name": cur["name"] or "unknown",
                    "input": _parse_tool_args(cur["args"]),
                }
            )

        self._send_json(
            200,
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "model": actual_model,
                "content": content,
                "stop_reason": "tool_use" if acc.has_calls() else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
            },
        )

    def _handle_stream(self, catpaw_resp, req_model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        msg_id = "msg_" + uuid.uuid4().hex
        actual_model = req_model
        usage_in = usage_out = 0
        acc = ToolCallAccumulator()

        block_index_of = {}
        next_ai = 1
        started = set()
        stopped = set()

        self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": req_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            },
        )
        self._sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )
        started.add(0)

        try:
            while True:
                raw = catpaw_resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                if d.get("content") == "[DONE]":
                    break
                sc = d.get("statusCode")
                if sc not in (None, 0, "0"):
                    msg = d.get("msg") or d.get("message") or d.get("content") or "catpaw error"
                    self._sse(
                        "error",
                        {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": "upstream statusCode=%s: %s" % (sc, msg),
                            },
                        },
                    )
                    self.close_connection = True
                    return
                if "model" in d and d.get("model"):
                    actual_model = d["model"]
                if "usage" in d:
                    u = d["usage"]
                    usage_in = u.get("prompt_tokens", usage_in)
                    usage_out = u.get("completion_tokens", usage_out)

                choices = d.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                content = delta.get("content")
                if content:
                    self._sse(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content}},
                    )

                for oi, fragment in acc.add(delta.get("tool_calls")):
                    ai = block_index_of.get(oi)
                    if ai is None:
                        ai = next_ai
                        next_ai += 1
                        block_index_of[oi] = ai
                    if ai not in started:
                        started.add(ai)
                        cur = acc.calls[oi]
                        self._sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": ai,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": cur["id"] or ("toolu_" + uuid.uuid4().hex[:10]),
                                    "name": cur["name"] or "unknown",
                                    "input": {},
                                },
                            },
                        )
                    self._sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": ai,
                            "delta": {"type": "input_json_delta", "partial_json": fragment},
                        },
                    )
        except Exception as e:
            self._sse(
                "error",
                {"type": "error", "error": {"type": "api_error", "message": "stream error: %s" % e}},
            )
            self.close_connection = True
            return

        for ai in sorted(list(started)):
            if ai not in stopped:
                self._sse("content_block_stop", {"type": "content_block_stop", "index": ai})
                stopped.add(ai)

        self._sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use" if acc.has_calls() else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage_out},
            },
        )
        self._sse("message_stop", {"type": "message_stop"})
        self.close_connection = True

    # ---------- agent 响应转换（工具闭环） ----------

    def _agent_events_blocks(self, catpaw_resp, sess, first_ev=None):
        """遍历 agent SSE，把累计式 content 转成增量事件。

        返回 generator，逐次 yield ("text", delta) 或
        ("tool_start", callId, name) 或 ("tool_delta", callId, delta)。
        内部维护累计状态。first_ev 为 agent_stream 已预读的首个事件。
        """
        text_prev = ""
        calls = {}
        next_ai = 1

        def _iter():
            if first_ev is not None:
                yield first_ev
            yield from iter_agent_events(catpaw_resp)

        for ev in _iter():
            msg = ev.get("message") or {}
            for blk in msg.get("content") or []:
                if blk.get("type") == "text" and blk.get("text"):
                    full = blk["text"]
                    if full.startswith(text_prev):
                        delta = full[len(text_prev):]
                        if delta:
                            text_prev = full
                            yield ("text", delta)
                elif blk.get("type") == "tool_use":
                    callId = blk.get("toolCallId") or ("call_" + uuid.uuid4().hex[:24])
                    name = blk.get("toolName") or ""
                    params = blk.get("toolParams") or ""
                    entry = calls.get(callId)
                    if not entry:
                        entry = {"name": name, "prev": "", "ai": next_ai, "started": False}
                        next_ai += 1
                        calls[callId] = entry
                        if callId:
                            sess["callnames"][callId] = name
                    elif name and not entry["name"]:
                        entry["name"] = name
                    if entry["name"] and not entry["started"]:
                        entry["started"] = True
                        yield ("tool_start", callId, entry["name"], entry["ai"])
                    if params and params != entry["prev"]:
                        delta = params[len(entry["prev"]):] if params.startswith(entry["prev"]) else params
                        entry["prev"] = params
                        if delta:
                            yield ("tool_delta", callId, delta, entry["ai"])
            usage = (ev.get("contextInfo") or {}).get("usage")
            if usage:
                yield ("usage", usage)
            if msg.get("finished"):
                return
        return calls

    def _handle_agent_stream(self, catpaw_resp, req_model, sess, items, full_sig, first_ev=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        msg_id = "msg_" + uuid.uuid4().hex
        usage_in = usage_out = 0
        started_blocks = set()
        finished_ok = False
        # text block 固定 index 0；工具块从 1 开始
        try:
            self._sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": req_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 1},
                    },
                },
            )
            self._sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
            started_blocks.add(0)

            has_tool_use = False
            for kind, *args in self._agent_events_blocks(catpaw_resp, sess, first_ev=first_ev):
                if kind == "text":
                    self._sse(
                        "content_block_delta",
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": args[0]}},
                    )
                elif kind == "tool_start":
                    callId, name, ai = args
                    has_tool_use = True
                    started_blocks.add(ai)
                    self._sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": ai,
                            "content_block": {
                                "type": "tool_use",
                                "id": callId,
                                "name": name,
                                "input": {},
                            },
                        },
                    )
                elif kind == "tool_delta":
                    callId, delta, ai = args
                    self._sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": ai,
                            "delta": {"type": "input_json_delta", "partial_json": delta},
                        },
                    )
                elif kind == "usage":
                    u = args[0]
                    usage_in = u.get("prompt_tokens", usage_in)
                    usage_out = u.get("completion_tokens", usage_out)

            for ai in sorted(started_blocks):
                self._sse("content_block_stop", {"type": "content_block_stop", "index": ai})

            self._sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use" if has_tool_use else "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": usage_out},
                },
            )
            self._sse("message_stop", {"type": "message_stop"})
            finished_ok = True
        except Exception as e:
            # 客户端可能已断开（BrokenPipe），错误事件只是尽力而为。
            try:
                self._sse(
                    "error",
                    {"type": "error", "error": {"type": "api_error", "message": "agent stream error: %s" % e}},
                )
            except Exception:
                pass
            self.close_connection = True
            return
        finally:
            self.close_connection = True
            if finished_ok:
                _finish_agent_session(sess, items, full_sig)
            else:
                # 任何失败（含客户端断开）都废弃本地会话：上游 conversationId 可能
                # 还残留一个进行中的 turn，复用会撞上“会话正在被处理”。下次请求会
                # 以新 conversationId 走快照导入，自动自愈。
                _drop_agent_session(sess)
            try:
                catpaw_resp.close()
            except Exception:
                pass

    def _handle_agent_non_stream(self, catpaw_resp, req_model, sess, items, full_sig, first_ev=None):
        text = ""
        tool_uses = []
        usage_in = usage_out = 0
        finished_ok = False
        try:
            for kind, *args in self._agent_events_blocks(catpaw_resp, sess, first_ev=first_ev):
                if kind == "text":
                    text += args[0]
                elif kind == "tool_start":
                    callId, name, ai = args
                    tool_uses.append({"id": callId, "name": name, "params": ""})
                elif kind == "tool_delta":
                    callId, delta, ai = args
                    for tu in tool_uses:
                        if tu["id"] == callId:
                            tu["params"] += delta
                            break
                elif kind == "usage":
                    u = args[0]
                    usage_in = u.get("prompt_tokens", usage_in)
                    usage_out = u.get("completion_tokens", usage_out)

            content = []
            if text:
                content.append({"type": "text", "text": text})
            for tu in tool_uses:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tu["id"],
                        "name": tu["name"],
                        "input": _parse_tool_args(tu["params"]),
                    }
                )

            self._send_json(
                200,
                {
                    "id": "msg_" + uuid.uuid4().hex,
                    "type": "message",
                    "role": "assistant",
                    "model": req_model,
                    "content": content,
                    "stop_reason": "tool_use" if tool_uses else "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
                },
            )
            finished_ok = True
        except Exception as e:
            try:
                self._anthropic_error(502, "api_error", "agent stream error: %s" % e)
            except Exception:
                pass
        finally:
            if finished_ok:
                _finish_agent_session(sess, items, full_sig)
            else:
                _drop_agent_session(sess)
            try:
                catpaw_resp.close()
            except Exception:
                pass


def main():
    global HOST, PORT
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            PORT = int(args[i + 1])
            i += 2
        elif args[i] == "--host" and i + 1 < len(args):
            HOST = args[i + 1]
            i += 2
        else:
            i += 1

    token, _ = get_token()
    log("CatPaw Anthropic Relay 启动")
    log("  监听: %s:%d" % (HOST, PORT))
    log("  gateway: %s (纯文本)" % UPSTREAM)
    log("  agent:   %s (工具闭环)" % AGENT_BASE)
    log("  SSO token: %s" % ("已加载" if token else "未找到（请先登录 CatPaw IDE）"))
    log("  默认模型: %s" % DEFAULT_MODEL)

    srv = ThreadingHTTPServer((HOST, PORT), RelayHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()
