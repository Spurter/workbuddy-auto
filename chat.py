#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 中国版每日对话一次（免费模型 quick-answer）。

背景：中国版成长中心的「活跃登录」（活跃日历/连登）需要每天真的用一次，
光签到 +100 不足以点亮当天。国际版上已验证：接口问 1+1 当天不计分、
分数隔夜结算、完成一次对话任务也不认接口聊天；中国版是否认接口聊天，
要看跑起来之后第二天的活跃日历——先加上再验证。

做法：每天调一次中国版聊天接口问一个简单问题，模型固定用 quick-answer。
实测（2026-11-24）：该模型在配置里的 credits 是 x0.00，问 1+1 返回
“等于 2”，usage 里 credit=0，一分钱不花。不要换别的模型——
deepseek-v4.1-flash 是 x0.11、glm-5.3-flash 是 x0.08，都会扣额度。

两种运行方式：
1) 云端（GitHub Actions / 任意 CI）：通过环境变量注入令牌
     WB_TOKEN   accessToken（必填，存在仓库 Secret 中）
     WB_DOMAIN  可选，默认 www.codebuddy.cn
2) 本机直接运行：未设置 WB_TOKEN 时，自动读取 WorkBuddy 桌面端登录态文件
     Windows: %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
     macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

接口（2026-11-24 实测走通）：
  POST https://www.codebuddy.cn/v2/chat/completions
  stream=true，第一条消息必须是 system，另需三个 fresh-UUID 头：
  X-Conversation-ID / X-Conversation-Request-ID / X-Conversation-Message-ID，
  缺了会 400（国际版上是 code 11128，国内版同理）。
  响应是 SSE，最后一块 data 里有 usage（含 credit）。

推送复用 checkin.py 的通道，PUSH_LEVEL=action：成功不推，只在出错时推，
手动触发勾选 force_notify 可强制推一条验证。每天只跑一次，重复跑也没事
（同一天多问一次也是 0 积分），所以不做去重。

退出码：0 = 对话成功（拿到回答）；1 = 失败（令牌失效 / 网络异常 / 未知错误）
安全约定：全程不打印、不落盘任何令牌内容。
"""

import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import checkin as _checkin  # 复用推送通道（notify / is_forced），不跑它的 main

DEFAULT_DOMAIN = "www.codebuddy.cn"
CHAT_PATH = "/v2/chat/completions"
MODEL = "quick-answer"  # 配置里 credits x0.00 的免费模型，别换
TIMEOUT = 90

LOCAL_FILES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info"),
    os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
]

# 每天换一个简单问题，按年积日轮换；都是不用动脑的短问答
QUESTIONS = [
    "1+1等于几？",
    "今天星期几？",
    "你好",
    "2+3等于几？",
    "讲一个一句话冷笑话",
    "10减4等于几？",
    "太阳从哪边升起？",
]


def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08")


def pick_question():
    day = datetime.now(timezone(timedelta(hours=8))).timetuple().tm_yday
    return QUESTIONS[day % len(QUESTIONS)]


def read_local_credentials():
    """读取本机登录态文件，返回 (token, domain, path) 或 (None, None, None)。"""
    for path in LOCAL_FILES:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    auth = (json.load(fh) or {}).get("auth") or {}
            except Exception:
                continue
            token = auth.get("accessToken")
            if token:
                return token, (auth.get("domain") or DEFAULT_DOMAIN).strip(), path
    return None, None, None


def get_token():
    token = os.environ.get("WB_TOKEN", "").strip()
    if token:
        return token, os.environ.get("WB_DOMAIN", "").strip() or DEFAULT_DOMAIN, "env"
    return read_local_credentials()


def chat(domain, token, question):
    """调一次聊天接口，返回 (reply, credit, total_tokens)；失败抛错。"""
    payload = {
        "model": MODEL,
        "stream": True,
        "messages": [
            {"role": "system", "content": "你是一个助手。"},
            {"role": "user", "content": question},
        ],
    }
    req = urllib.request.Request(
        "https://" + domain + CHAT_PATH,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1",
            "X-Domain": domain,
            "Origin": "https://" + domain,
            "Referer": "https://" + domain + "/",
            "X-Conversation-ID": str(uuid.uuid4()),
            "X-Conversation-Request-ID": str(uuid.uuid4()),
            "X-Conversation-Message-ID": str(uuid.uuid4()),
        },
    )
    reply_parts = []
    credit = None
    total_tokens = None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError("HTTP %s" % resp.status)
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        reply_parts.append(text)
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    credit = usage.get("credit")
                    total_tokens = usage.get("total_tokens")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "ignore")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        raise RuntimeError("HTTP %s code=%s msg=%s" % (
            err.code, body.get("code"), (body.get("msg") or raw)[:200]))
    except Exception as err:
        raise RuntimeError("%s: %s" % (type(err).__name__, err))
    reply = "".join(reply_parts).strip()
    if not reply:
        raise RuntimeError("接口返回了空回答")
    return reply, credit, total_tokens


PUSH_LEVEL = os.environ.get("PUSH_LEVEL", "action").strip().lower()


def should_push(result):
    if _checkin.is_forced():
        return True
    if PUSH_LEVEL == "off":
        return False
    # action（默认）：只在出错时推；成功每天一次不值得推
    return result.get("status") == "error"


def build_title(result):
    label = os.environ.get("WB_LABEL", "").strip()
    prefix = ("【%s】" % label if label else "") + ("（测试）" if _checkin.is_forced() else "")
    if result.get("status") == "error":
        return prefix + "每日对话异常，需要处理"
    return prefix + "每日对话"


def build_body(result):
    lines = [
        "时间：%s" % result.get("time", ""),
        "问题：%s" % result.get("question", ""),
    ]
    if result.get("status") == "error":
        lines.append("结果：异常")
        if result.get("msg"):
            lines.append("原因：%s" % result["msg"])
    else:
        lines.append("结果：对话成功")
        lines.append("回答：%s" % (result.get("reply") or "")[:100])
        if result.get("credit") is not None:
            lines.append("本次消耗：%s 积分" % result["credit"])
    return "\n".join(lines)


def emit(result):
    print(json.dumps(result, ensure_ascii=False))
    if should_push(result):
        _checkin.report_push(_checkin.notify(build_title(result), build_body(result)))


def main():
    token, domain, source = get_token()
    if not token:
        emit({"result": "CHAT", "status": "error", "action": "chat",
              "time": now_cn(), "source": source or "none",
              "msg": "没有可用的 WB_TOKEN（环境变量或本机登录态都没有）"})
        return 1
    question = pick_question()
    try:
        reply, credit, total_tokens = chat(domain, token, question)
    except Exception as err:
        emit({"result": "CHAT", "status": "error", "action": "chat",
              "time": now_cn(), "source": source, "model": MODEL,
              "question": question, "msg": str(err)})
        return 1
    emit({"result": "CHAT", "status": "ok", "action": "chatted",
          "time": now_cn(), "source": source, "model": MODEL,
          "question": question, "reply": reply[:200],
          "credit": credit, "tokens": total_tokens,
          "msg": "对话成功：%s，消耗 %s 积分" % (reply[:60], credit)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
