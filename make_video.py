#!/usr/bin/env python3
"""ご当地ソング動画をつくる

  python make_video.py configs/shonan.json                 # 動画を書き出す（out/ に保存）
  python make_video.py configs/shonan.json --check         # 地名の解決結果だけ確認
  python make_video.py configs/shonan.json --preview 2 6   # 2秒目と6秒目を PNG で確認
"""
import argparse, json, os, re, sys, time
from gotouchi.render import Video

def main():
    ap = argparse.ArgumentParser(description="「〇〇のご当地ソング」縦型動画をつくる")
    ap.add_argument("config", help="ビルダーで作った config の JSON")
    ap.add_argument("-o", "--out", help="出力ファイル（省略時は out/<タイトル>.mp4）")
    ap.add_argument("--check", action="store_true", help="地名の解決結果を表示して終わる")
    ap.add_argument("--preview", nargs="*", type=float, help="指定した秒のコマを PNG で書き出す")
    ap.add_argument("--offline", action="store_true", help="ネットの地名検索を使わない")
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    v = Video(cfg, online=not a.offline)
    if a.check:
        return
    os.makedirs("out", exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", cfg.get("title", "video")) or "video"
    out = a.out or os.path.join("out", f"{stem}_gotouchi.mp4")
    if a.preview is not None:
        ts = a.preview or [1.5] + [s["t0"] + s["dur"]*0.75 for s in v.songs] + [v.total - 0.5]
        for p in v.write(out, preview=ts): print("  ", p)
        return
    t = time.time()
    v.write(out)
    print(f"✔ {out}  ({v.total:.1f}秒, 書き出し {time.time()-t:.0f}秒)")

if __name__ == "__main__":
    main()
