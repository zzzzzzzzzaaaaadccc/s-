"""安卓 ntfy 通知：标准库、后台发送、独立于本地铃声。"""
import json
import queue
import re
import secrets
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

SERVER = "https://ntfy.sh"
GUIDE = "https://docs.ntfy.sh/subscribe/phone/"


class PushError(Exception):
    pass


def new_topic():
    return "worlds-" + secrets.token_hex(16)


def send_notification(topic, title, message, opener=None):
    if not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", topic):
        raise PushError("订阅频道格式无效，请重新打开安卓推送设置。")
    body = {"topic": topic, "title": title, "message": message, "priority": 5,
            "tags": ["alarm_clock"], "click": "https://lolesports.com/en-US/?league=worlds"}
    request = urllib.request.Request(SERVER + "/", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(request, timeout=12) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise PushError("推送服务限流，请稍后手动重试。") from None
        raise PushError(f"推送服务返回 HTTP {exc.code}。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PushError("无法连接 ntfy.sh，请检查电脑网络或代理。") from None
    except (ValueError, TypeError):
        raise PushError("推送服务返回的数据无效。") from None
    if not isinstance(result, dict) or result.get("event") != "message" or not result.get("id") or result.get("topic") != topic:
        raise PushError("推送服务未确认接收，请稍后重试。")
    return result["id"]


class PhonePush:
    def __init__(self, parent, root, settings, save, compact=False):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root, self.settings, self.save = root, settings, save
        self.stopped = threading.Event()
        self.results = queue.Queue()
        self.dialog = None
        self.test_busy = False
        config = settings.get("phone_push")
        self.config = dict(config) if isinstance(config, dict) else {}
        initial = "手机推送已启用（尚未验证本次手机接收）" if self.config.get("enabled") else "手机推送未启用"
        self.status = tk.StringVar(value=initial)
        if compact:
            ttk.Button(parent, text="安卓手机推送设置", command=self.configure).pack(side="left", padx=(8, 0))
        else:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=(10, 0))
            ttk.Button(row, text="安卓手机推送设置", command=self.configure).pack(side="left")
            ttk.Label(row, textvariable=self.status, wraplength=620).pack(side="left", padx=12)
        self.poll_timer = root.after(150, self.poll)

    def configure(self):
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        tk, ttk = self.tk, self.ttk
        dialog = self.dialog = tk.Toplevel(self.root)
        dialog.title("连接安卓手机 · ntfy")
        dialog.geometry("750x530")
        dialog.minsize(700, 510)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="开赛后，手机收到声音 + 振动通知", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(frame, text="1. 在安卓手机安装 ntfy，允许通知、声音、振动和后台运行。", wraplength=690).pack(anchor="w", pady=(18, 5))
        ttk.Button(frame, text="打开官方安装说明（含 APK 下载入口）", command=lambda: webbrowser.open(GUIDE)).pack(anchor="w")
        ttk.Label(frame, text="2. 在手机 ntfy 中点 + 订阅频道，服务器填写 https://ntfy.sh。", wraplength=690).pack(anchor="w", pady=(18, 6))
        topic = self.config.get("topic")
        if not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", topic):
            topic = new_topic()
        self.config["topic"] = topic
        value = tk.StringVar(value=topic)
        ttk.Entry(frame, textvariable=value, state="readonly", width=60, font=("Consolas", 12)).pack(fill="x")
        copy_row = ttk.Frame(frame)
        copy_row.pack(fill="x", pady=6)
        copied = tk.StringVar(value="随机频道名，请勿公开；知道名字的人可以收发此频道通知。")
        def copy_topic():
            dialog.clipboard_clear()
            dialog.clipboard_append(topic)
            copied.set("已复制频道名。请将它填入手机 ntfy 的「Topic name / 主题名」。")
        ttk.Button(copy_row, text="复制频道名", command=copy_topic).pack(side="left")
        ttk.Label(frame, textvariable=copied, wraplength=690, foreground="#666666").pack(anchor="w")
        ttk.Label(frame, text="3. 订阅完成后启用推送，点击测试；手机收到声音和振动才算接通。", wraplength=690).pack(anchor="w", pady=(18, 5))
        enabled = tk.BooleanVar(value=bool(self.config.get("enabled")))
        ttk.Checkbutton(frame, text="比赛开赛时推送到安卓手机", variable=enabled).pack(anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=12)
        def persist():
            self.config = {"enabled": bool(enabled.get()), "topic": topic}
            self.settings["phone_push"] = dict(self.config)
            saved = self.save()
            self.status.set("手机推送已启用，请用测试通知确认手机接收" if enabled.get() else "手机推送未启用")
            return saved is not False
        def save_close():
            if persist():
                dialog.destroy()
        def test():
            if self.test_busy:
                return
            if persist():
                self.send("S 赛闹钟 · 手机连接测试", "这是一条测试通知。请确认手机有声音和振动，然后回到电脑选择比赛并开始监控。", test=True)
        ttk.Button(actions, text="保存设置", command=save_close).pack(side="left")
        ttk.Button(actions, text="发送手机测试通知", command=test).pack(side="left", padx=10)
        ttk.Label(frame, textvariable=self.status, wraplength=690, foreground="#087f5b").pack(anchor="w")
        ttk.Label(frame, text="最高优先级 5；实际声音、振动和勿扰行为由安卓通知设置控制。电脑和手机都须联网；电脑脚本须持续运行。", wraplength=690, foreground="#666666").pack(anchor="w", pady=(12, 0))

    def notify(self, matches):
        if not self.config.get("enabled"):
            return
        title = "、".join(m.title for m in matches)
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
        self.send("S 赛已开打：" + title, f"官网已显示比赛进行中：{title}。\n检测时间：北京时间 {now}。\n点击通知可打开赛事官网。")

    def send(self, title, message, test=False):
        topic = self.config.get("topic", "")
        if test:
            self.test_busy = True
        self.status.set("正在发送手机测试通知……" if test else "开赛提醒正在推送到手机……")
        def work():
            # 单次发送避免网络超时后重发造成重复振动；失败明确显示供用户重试。
            try:
                if not self.stopped.is_set():
                    send_notification(topic, title, message)
                    self.results.put((test, "推送服务已接收；请在手机确认通知、声音和振动。"))
            except PushError as exc:
                self.results.put((test, "手机推送失败：" + str(exc) + " 可在设置中重试测试通知。"))
            except Exception:
                self.results.put((test, "手机推送失败：发生意外错误，请重新测试。"))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if self.stopped.is_set():
            return
        while True:
            try:
                test, status = self.results.get_nowait()
            except queue.Empty:
                break
            if test:
                self.test_busy = False
            self.status.set(status)
        self.poll_timer = self.root.after(150, self.poll)

    def close(self):
        self.stopped.set()
        self.root.after_cancel(self.poll_timer)
