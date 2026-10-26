#!/usr/bin/env python3
"""ご当地ソング動画スタジオ（ブラウザで使うローカルサーバー）

  python3 app.py            # http://localhost:8000 で開く
  python3 app.py --port 8080

ブラウザで曲を選び →「動画を作る」→ 書き出しが終わったらそのままダウンロード。
同じネットワークの別のPCやスマホからも、表示されるアドレスで開ける。
追加のライブラリは不要（Python 標準の http.server を使う）。
"""
import argparse, json, os, re, socket, subprocess, sys, threading, time, traceback, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "out")
PREV = os.path.join(OUT, "preview")
CONFIGS = os.path.join(ROOT, "configs")
BUILDER = os.path.join(ROOT, "builder", "index.html")

JOBS = {}                     # job_id → 状態
RENDER_LOCK = threading.Lock()  # 動画の書き出しは1本ずつ


def slug(t):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", t or "video").strip("_") or "video"


def make_video(cfg):
    from gotouchi.render import Video
    return Video(cfg, online=True, verbose=False)


# ------------------------------------------------------------------ 書き出し
def run_job(job_id, cfg):
    job = JOBS[job_id]
    try:
        job.update(state="準備中", progress=0.0)
        v = make_video(cfg)
        job["summary"] = v.summary()
        with RENDER_LOCK:
            job.update(state="書き出し中")
            name = f"{slug(cfg.get('title'))}_gotouchi_{time.strftime('%m%d_%H%M%S')}.mp4"
            path = os.path.join(OUT, name)
            t0 = time.time()

            def prog(x):
                job["progress"] = round(x, 3)
                if x > 0.02:
                    job["eta"] = int((time.time() - t0) * (1 - x) / x)
            v.write(path, on_progress=prog)
        job.update(state="完了", progress=1.0, file=name, eta=0,
                   size=os.path.getsize(path), took=int(time.time() - t0))
    except Exception as e:
        job.update(state="エラー", error=str(e), trace=traceback.format_exc()[-1500:])


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "GotouchiStudio/1.0"

    def log_message(self, fmt, *args):
        if "/api/job/" not in self.path:   # 進み具合の問い合わせはログに出さない
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    # ---- 返す
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def send_file(self, path, ctype, download=None):
        if not os.path.isfile(path):
            return self.send_json({"error": "ファイルがありません"}, 404)
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode() or "{}")

    # ---- GET
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self.send_file(BUILDER, "text/html; charset=utf-8")
        if path == "/api/ping":
            return self.send_json({"ok": True, "busy": RENDER_LOCK.locked()})
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            return self.send_json(job or {"error": "ジョブが見つかりません"}, 200 if job else 404)
        if path == "/api/videos":
            os.makedirs(OUT, exist_ok=True)
            vids = sorted((f for f in os.listdir(OUT) if f.endswith(".mp4")),
                          key=lambda f: os.path.getmtime(os.path.join(OUT, f)), reverse=True)
            return self.send_json([{"file": f, "size": os.path.getsize(os.path.join(OUT, f)),
                                    "time": time.strftime("%m/%d %H:%M", time.localtime(os.path.getmtime(os.path.join(OUT, f))))}
                                   for f in vids[:30]])
        if path == "/api/configs":
            os.makedirs(CONFIGS, exist_ok=True)
            fs = sorted((f for f in os.listdir(CONFIGS) if f.endswith(".json")),
                        key=lambda f: os.path.getmtime(os.path.join(CONFIGS, f)), reverse=True)
            return self.send_json(fs)
        if path.startswith("/api/config/"):
            name = os.path.basename(path)
            fp = os.path.join(CONFIGS, name)
            if not os.path.isfile(fp):
                return self.send_json({"error": "ファイルがありません"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))
        if path.startswith("/video/"):
            name = os.path.basename(path)
            return self.send_file(os.path.join(OUT, name), "video/mp4")
        if path.startswith("/download/"):
            name = os.path.basename(path)
            return self.send_file(os.path.join(OUT, name), "video/mp4", download=name)
        if path.startswith("/preview/"):
            return self.send_file(os.path.join(PREV, os.path.basename(path)), "image/png")
        self.send_json({"error": "not found"}, 404)

    # ---- POST
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            cfg = self.read_json()
        except Exception as e:
            return self.send_json({"error": f"JSON が読めません: {e}"}, 400)
        if not cfg.get("songs"):
            return self.send_json({"error": "曲が選ばれていません"}, 400)
        try:
            if path == "/api/check":
                return self.send_json(make_video(cfg).summary())
            if path == "/api/preview":
                v = make_video(cfg)
                os.makedirs(PREV, exist_ok=True)
                for f in os.listdir(PREV):
                    os.remove(os.path.join(PREV, f))
                ts = [1.5] + [s["t0"] + s["dur"] * 0.7 for s in v.songs] + [v.total - 0.5]
                stem = os.path.join(PREV, f"p{int(time.time())}")
                paths = v.write(stem + ".mp4", preview=ts)
                return self.send_json({"summary": v.summary(),
                                       "images": ["/preview/" + os.path.basename(p) for p in paths]})
            if path == "/api/save":
                os.makedirs(CONFIGS, exist_ok=True)
                name = slug(cfg.get("title")) + ".json"
                with open(os.path.join(CONFIGS, name), "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                return self.send_json({"saved": "configs/" + name})
            if path == "/api/render":
                job_id = uuid.uuid4().hex[:10]
                JOBS[job_id] = {"id": job_id, "state": "待機中", "progress": 0.0,
                                "title": cfg.get("title", ""), "queued": RENDER_LOCK.locked()}
                threading.Thread(target=run_job, args=(job_id, cfg), daemon=True).start()
                return self.send_json({"job": job_id})
        except SystemExit as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)
        self.send_json({"error": "not found"}, 404)


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ips.add(s.getsockname()[0]); s.close()
    except OSError:
        pass
    try:
        for ip in subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split():
            if ":" not in ip: ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def main():
    ap = argparse.ArgumentParser(description="ご当地ソング動画スタジオ")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0", help="127.0.0.1 にするとこのPCからだけ開ける")
    a = ap.parse_args()
    # ビルダーを最新のデータで作り直す
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_builder.py")], check=False)
    os.makedirs(OUT, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("\n■ ご当地ソング動画スタジオ を起動しました")
    print(f"  このPCで開く:   http://localhost:{a.port}")
    if a.host == "0.0.0.0":
        for ip in local_ips():
            print(f"  ほかの端末から: http://{ip}:{a.port}")
    print("  止めるときは Ctrl+C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました")


if __name__ == "__main__":
    main()
