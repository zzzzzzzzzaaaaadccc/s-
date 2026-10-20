"""S 赛开赛闹钟。Python 3.10+；仅使用标准库。"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import queue
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from android_push import PhonePush

BASE = Path(__file__).resolve().parent
SETTINGS = BASE / "settings.json"
CST = timezone(timedelta(hours=8))
ENDPOINT = "https://lolesports.com/api/gql"
WORLD_LEAGUE = "98767975604431411"
# 官网 2026-10-20 发布的公开前端查询清单；不是私人 API 密钥。
QUERY_ID = "f5a7bb64c62eca0f19b048a5835197e8f322b47170a1772ba47bffc0945310ce"
CLIENT_VERSION = "f982731"
STATE_TEXT = {"unstarted": "未开始", "inProgress": "正在进行", "completed": "已结束"}


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class Match:
    id: str
    start: str
    state: str
    teams: tuple[str, ...]
    stage: str = ""

    @property
    def title(self):
        return " vs ".join(self.teams) or "对阵待定"

    @property
    def time_text(self):
        try:
            return datetime.fromisoformat(self.start.replace("Z", "+00:00")).astimezone(CST).strftime("%m-%d %H:%M")
        except ValueError:
            return "时间待定"

    def saved(self):
        return {"id": self.id, "start": self.start, "state": self.state,
                "teams": list(self.teams), "stage": self.stage}


def parse_page(payload):
    if not isinstance(payload, dict):
        raise FeedError("官网返回了无法识别的数据")
    if payload.get("errors"):
        message = "; ".join(str(e.get("message", e)) for e in payload["errors"])
        raise FeedError("官网接口错误：" + message[:240])
    try:
        page = payload["data"]["esports"]
        events = page["events"]
        pages = page["pages"]
        if not isinstance(events, list) or not isinstance(pages, dict):
            raise TypeError()
        result = []
        for event in events:
            if event.get("type") != "match":
                continue
            if str((event.get("league") or {}).get("id")) != WORLD_LEAGUE:
                continue
            # 未知状态必须报错，不能误判为未开赛或根据预定时间猜测。
            if event["state"] not in STATE_TEXT:
                raise FeedError("官网出现未知比赛状态：" + str(event["state"]))
            teams = tuple(t.get("code") or t.get("name") or "TBD"
                          for t in event.get("matchTeams", []))
            result.append(Match(str(event["id"]), event["startTime"], event["state"],
                                teams, event.get("blockName") or ""))
        return result, pages
    except (KeyError, TypeError, AttributeError) as exc:
        raise FeedError("官网赛程格式已改变，请更新脚本") from exc


class Feed:
    def __init__(self, settings=None, opener=None):
        settings = settings or {}
        self.query_id = settings.get("query_id", QUERY_ID)
        self.client_version = settings.get("client_version", CLIENT_VERSION)
        self.opener = opener or urllib.request.urlopen

    def page(self, variables):
        params = {"operationName": "scheduleEvents", "variables": json.dumps(variables),
                  "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": self.query_id}})}
        request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode(params), headers={
            "User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
            "Accept": "application/json", "Referer": "https://lolesports.com/en-US/",
            "Cache-Control": "no-cache", "apollographql-client-name": "Esports Web",
            "apollographql-client-version": self.client_version,
        })
        try:
            with self.opener(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404):
                raise FeedError(f"官网接口不可用（HTTP {exc.code}），可能需要更新接口版本；不会按预定时间误报") from exc
            raise FeedError(f"官网暂时不可用（HTTP {exc.code}），将自动重试") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise FeedError(f"连接官网失败：{exc}；请检查网络或系统代理") from exc
        return parse_page(payload)

    def fetch(self, targets=()):
        today = datetime.now(CST).date()
        start = today - timedelta(days=2)
        end = today + timedelta(days=90)
        for match in targets:
            try:
                day = datetime.fromisoformat(match.start.replace("Z", "+00:00")).date()
                start = min(start, day - timedelta(days=2))
                end = max(end, day + timedelta(days=7))
            except ValueError:
                pass
        variables = {"hl": "en_US", "sport": ["lol"], "leagues": [WORLD_LEAGUE],
                     "eventType": "match", "pageSize": 100,
                     "eventDateStart": start.isoformat(), "eventDateEnd": end.isoformat()}
        pending, visited, matches = [None], set(), {}
        while pending:
            token = pending.pop(0)
            if token in visited:
                continue
            if len(visited) >= 20:
                raise FeedError("赛程分页过多，未能完整读取；请移除过旧比赛后重试")
            visited.add(token)
            values = dict(variables)
            if token:
                values["pageToken"] = token
            found, pages = self.page(values)
            matches.update((m.id, m) for m in found)
            for direction in ("older", "newer"):
                next_token = pages.get(direction)
                if next_token and next_token not in visited:
                    pending.append(next_token)
        return sorted(matches.values(), key=lambda m: (m.start, m.id))


class Watcher:
    """精确绑定赛事 ID；响铃只由官网 inProgress 状态触发。"""
    def __init__(self, matches=()):
        self.pending = {m.id: m for m in matches}

    def update(self, matches):
        current = {m.id: m for m in matches}
        ringing, ended, missing = [], [], []
        for event_id in list(self.pending):
            match = current.get(event_id)
            if match is None:
                missing.append(self.pending[event_id])
                continue
            self.pending[event_id] = match
            if match.state == "inProgress":
                ringing.append(match)
                del self.pending[event_id]
            elif match.state == "completed":
                ended.append(match)
                del self.pending[event_id]
        return ringing, ended, missing


def make_wave(path):
    rate = 22050
    samples = bytearray()
    for index in range(rate * 2):
        t = index / rate
        phase = t % 0.5
        envelope = min(1, phase / 0.02, max(0, (0.32 - phase) / 0.02))
        tone = 880 if int(t * 2) % 2 == 0 else 1175
        value = int(16000 * envelope * math.sin(2 * math.pi * tone * t))
        samples.extend(struct.pack("<h", value))
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(samples)


def load_settings():
    if not SETTINGS.exists():
        return {}, ""
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("配置必须是 JSON 对象")
        return settings, ""
    except (OSError, ValueError) as exc:
        return {}, f"未能读取上次设置，将使用默认值：{exc}"


class App:
    def __init__(self, root, settings, warning=""):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root, self.settings = root, settings
        self.feed = Feed(settings)
        self.events = {}
        self.watcher = Watcher()
        self.active = False
        self.generation = 0
        self.busy = False
        self.closed = False
        self.errors = 0
        self.timer = None
        self.test_timer = None
        self.ringing = False
        self.testing = False
        self.results = queue.Queue()
        self.last_targets = []
        try:
            self.last_targets = [Match(str(m["id"]), m["start"], m.get("state", "unstarted"),
                                       tuple(m.get("teams", [])), m.get("stage", ""))
                                 for m in settings.get("targets", [])]
        except (KeyError, TypeError, AttributeError):
            warning = "上次选择的比赛信息无效，请重新选择。"
        root.title("S 赛开赛闹钟 · LoL Worlds")
        root.geometry("960x700")
        root.minsize(900, 650)
        root.configure(bg="#f4f6fa")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=32)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        main = ttk.Frame(root, padding=22)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="S 赛开打，叫我一声", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ttk.Label(main, text="选择对阵 → 开始监控 → 官网显示进行中时响铃。时间均为北京时间。").pack(anchor="w", pady=(6, 16))
        tools = ttk.Frame(main)
        tools.pack(fill="x")
        ttk.Label(tools, text="筛选战队").pack(side="left")
        self.search = tk.StringVar()
        ttk.Entry(tools, textvariable=self.search, width=20).pack(side="left", padx=8)
        self.search.trace_add("write", lambda *_: self.render())
        self.refresh_button = ttk.Button(tools, text="刷新赛程", command=self.refresh)
        self.refresh_button.pack(side="left", padx=4)
        ttk.Label(tools, text="检查间隔（秒）").pack(side="left", padx=(20, 4))
        self.interval = tk.StringVar(value=str(settings.get("interval", 30)))
        ttk.Spinbox(tools, from_=15, to=300, increment=5, width=5, textvariable=self.interval).pack(side="left")
        self.show_ended = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="显示已结束", variable=self.show_ended, command=self.render).pack(side="right")
        list_frame = ttk.Frame(main)
        list_frame.pack(fill="both", expand=True, pady=14)
        self.tree = ttk.Treeview(list_frame, columns=("time", "teams", "stage", "state"), show="headings", selectmode="extended")
        for key, label, width in [("time", "北京时间", 125), ("teams", "对阵", 250), ("stage", "阶段", 240), ("state", "状态", 110)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=90, anchor="w")
        self.tree.tag_configure("live", foreground="#087f5b")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(main)
        row.pack(fill="x")
        ttk.Button(row, text="开始监控所选比赛", command=self.start).pack(side="left")
        ttk.Button(row, text="停止监控", command=self.stop).pack(side="left", padx=8)
        ttk.Button(row, text="试听 5 秒", command=self.test_sound).pack(side="right")
        ttk.Button(row, text="停止响铃", command=self.silence).pack(side="right", padx=8)
        # 手机推送入口要紧跟主操作按钮，避免在小屏幕或高 DPI 缩放时被底部裁掉。
        self.phone = PhonePush(row, root, self.settings, self.save, compact=True)
        self.alarm_text = tk.StringVar(value="可按住 Ctrl 选择多场；已开赛的比赛会立即提醒。")
        ttk.Label(main, textvariable=self.alarm_text, foreground="#b45309", wraplength=875,
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(16, 8))
        self.status = tk.StringVar(value=warning or "正在加载官方赛程……")
        ttk.Label(main, textvariable=self.status, wraplength=875).pack(anchor="w")
        ttk.Label(main, text="监控时可最小化；请保持联网和音量开启。程序会请求阻止自动休眠，手动睡眠或关机会中断提醒。",
                  foreground="#666666", wraplength=875).pack(anchor="w", pady=(12, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.consume)
        root.after(200, self.refresh)
        if warning:
            root.after(300, lambda: messagebox.showwarning("设置提示", warning))

    def save(self):
        self.settings.update(interval=self.seconds(), targets=[m.saved() for m in self.last_targets])
        try:
            temp = SETTINGS.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(SETTINGS)
            return True
        except OSError as exc:
            self.status.set(f"设置保存失败（当前监控仍可使用）：{exc}")
            logging.exception("Settings write failed")
            return False

    def seconds(self):
        try:
            return max(15, min(300, int(self.interval.get())))
        except ValueError:
            return 30

    def render(self):
        selected = set(self.tree.selection()) or {m.id for m in self.last_targets}
        self.tree.delete(*self.tree.get_children())
        query = self.search.get().strip().casefold()
        for match in sorted(self.events.values(), key=lambda m: (m.start, m.id)):
            if match.state == "completed" and not self.show_ended.get():
                continue
            if query and query not in match.title.casefold():
                continue
            self.tree.insert("", "end", iid=match.id, values=(match.time_text, match.title, match.stage,
                STATE_TEXT[match.state]), tags=("live",) if match.state == "inProgress" else ())
            if match.id in selected:
                self.tree.selection_add(match.id)

    def cancel_timer(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None

    def refresh(self):
        if self.busy:
            return
        self.cancel_timer()
        self.busy = True
        self.refresh_button.configure(state="disabled")
        generation = self.generation
        targets = tuple(self.watcher.pending.values()) if self.active else tuple(self.last_targets)
        self.status.set("正在连接官网，读取最新比赛状态……")
        def work():
            try:
                self.results.put((generation, self.feed.fetch(targets), None))
            except Exception as exc:
                logging.exception("Schedule fetch failed")
                self.results.put((generation, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def consume(self):
        if self.closed:
            return
        try:
            generation, matches, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.refresh_button.configure(state="normal")
            if generation != self.generation:
                # 开始/停止监控之后，丢弃之前请求的结果，避免旧请求触发提醒。
                self.refresh()
            elif error:
                self.errors += 1
                delay = min(120, self.seconds() * (2 ** min(self.errors - 1, 3)))
                self.status.set(f"第 {self.errors} 次读取失败：{error}。{delay} 秒后重试；当前状态未知。")
                self.timer = self.root.after(delay * 1000, self.refresh)
            else:
                self.errors = 0
                self.events = {m.id: m for m in matches}
                self.render()
                now = datetime.now(CST).strftime("%H:%M:%S")
                detail = f"最近成功检查 {now} · 共 {len(matches)} 场 S 赛"
                if self.active:
                    rings, ended, missing = self.watcher.update(matches)
                    if rings:
                        self.alarm(rings)
                    if ended:
                        detail += " · 已结束，未补响：" + "、".join(m.title for m in ended)
                    if missing:
                        detail += " · 暂未找到所选赛事，继续重试：" + "、".join(m.title for m in missing)
                    detail += f" · 等待 {len(self.watcher.pending)} 场"
                    if not self.watcher.pending:
                        self.active = False
                        self.keep_awake(False)
                        detail += " · 本次监控完成"
                    else:
                        self.timer = self.root.after(self.seconds() * 1000, self.refresh)
                else:
                    detail += " · 请选择比赛并开始监控"
                if not matches:
                    detail += "（官网尚未发布此日期范围内赛程）"
                self.status.set(detail)
        self.root.after(100, self.consume)

    def start(self):
        matches = [self.events[mid] for mid in self.tree.selection() if mid in self.events]
        matches = [m for m in matches if m.state != "completed"]
        if not matches:
            self.messagebox.showinfo("选择比赛", "请先在列表中选择至少一场未结束的比赛。")
            return
        self.silence()
        self.cancel_timer()
        self.generation += 1
        self.last_targets = matches
        self.watcher = Watcher(matches)
        self.active = True
        self.errors = 0
        self.interval.set(str(self.seconds()))
        self.save()
        self.alarm_text.set("正在等待：" + "、".join(m.title for m in matches))
        self.keep_awake(True)
        self.refresh()

    def stop(self):
        self.active = False
        self.generation += 1
        self.watcher = Watcher()
        self.cancel_timer()
        self.silence()
        self.keep_awake(False)
        self.alarm_text.set("已停止监控。重新选择比赛后可再次启动。")
        self.status.set("监控已停止")

    def keep_awake(self, enabled):
        try:
            result = ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000)
            if enabled and not result:
                self.messagebox.showwarning("电源提示", "无法阻止自动休眠，请在 Windows 电源设置中手动关闭睡眠。")
        except (AttributeError, OSError):
            if enabled:
                self.messagebox.showwarning("电源提示", "无法阻止自动休眠，请在系统电源设置中关闭睡眠。")

    def play(self):
        import winsound
        try:
            custom = self.settings.get("sound_file", "")
            path = Path(custom) if custom else BASE / "alarm.wav"
            if not path.is_absolute():
                path = BASE / path
            if custom and not path.is_file():
                raise OSError(f"找不到自定义铃声：{path}")
            if not custom and not path.exists():
                make_wave(path)
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC |
                               winsound.SND_LOOP | winsound.SND_NODEFAULT)
            self.ringing = True
            return True
        except (OSError, RuntimeError) as exc:
            logging.exception("Sound playback failed")
            self.messagebox.showerror("无法播放铃声", f"{exc}\n请检查输出设备，或移除 settings.json 中的自定义铃声设置。")
            return False

    def alarm(self, matches):
        if self.test_timer:
            self.root.after_cancel(self.test_timer)
            self.test_timer = None
        self.testing = False
        title = "、".join(m.title for m in matches)
        self.alarm_text.set("比赛已开打：" + title + "！点击「停止响铃」关闭声音。")
        logging.info("LIVE: %s", title)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(5000, lambda: self.root.attributes("-topmost", False))
        self.phone.notify(matches)
        self.play()

    def test_sound(self):
        if self.ringing and not self.testing:
            return
        self.silence()
        self.testing = True
        if self.play():
            self.test_timer = self.root.after(5000, self.silence)

    def silence(self):
        if self.test_timer:
            self.root.after_cancel(self.test_timer)
            self.test_timer = None
        if self.ringing:
            import winsound
            winsound.PlaySound(None, 0)
        self.ringing = False
        self.testing = False

    def close(self):
        self.closed = True
        self.phone.close()
        self.cancel_timer()
        self.silence()
        self.keep_awake(False)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="英雄联盟 S 赛开赛闹钟")
    parser.add_argument("--check", action="store_true", help="检查官网连通性并打印赛程，不播放声音")
    args = parser.parse_args()
    settings, warning = load_settings()
    if args.check:
        try:
            matches = Feed(settings).fetch()
        except FeedError as exc:
            print(str(exc))
            return 1
        print(f"官网连接成功，共 {len(matches)} 场 S 赛（北京时间）：")
        for match in matches:
            print(match.time_text, match.title, STATE_TEXT[match.state])
        return 0
    import tkinter as tk
    handler = RotatingFileHandler(BASE / "alarm.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    App(root, settings, warning)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
