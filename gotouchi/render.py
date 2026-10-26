"""「〇〇のご当地ソング」縦型動画レンダラー (1080x1920)

config(JSON) → 地名を点・エリア・線に変換 → pycairo で1コマずつ描いて ffmpeg に流す。
"""
import json, math, os, subprocess, sys
import cairo
import numpy as np
from shapely.geometry import box as sbox
from .world import World, _point_along

# ------------------------------------------------------------------ 色
def hexc(h, a=1.0):
    h = h.lstrip("#"); return (int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255, a)

BG = hexc("#07090c"); LAND = hexc("#1b2128"); BORDER = hexc("#313a45"); PREFBORDER = hexc("#4f5c6b")
TEAL = hexc("#3fd0bb"); ORANGE = hexc("#ff9a3c"); RIVER = hexc("#4aa3e0"); RAILC = hexc("#f2f5f7")
WHITE = hexc("#f2f5f7"); GREY = hexc("#8a96a3"); NOTE = hexc("#d5dbe1")
CARD = hexc("#10151b"); CARDLINE = hexc("#2b343e"); CHIP = hexc("#262e37"); HEADBG = hexc("#11161c")
BGRAIL = hexc("#323b46"); DARK = hexc("#0b0f13")

DEFAULTS = {
    "width": 1080, "height": 1920, "fps": 30, "font": "Noto Sans CJK JP",
    "intro_sec": 3.5, "song_sec": 5.5, "outro_sec": 5.0, "move_sec": 1.1,
    "point_span_km": 12, "header_note": "",
}


def ease(t):
    t = max(0.0, min(1.0, t)); return t*t*(3-2*t)


class Video:
    def __init__(self, cfg, online=True, verbose=True):
        self.cfg = cfg
        self.opt = {**DEFAULTS, **cfg.get("options", {})}
        self.W, self.H, self.FPS = self.opt["width"], self.opt["height"], self.opt["fps"]
        self.HEADER = 170
        self.FONT = self.opt["font"]
        self.verbose = verbose
        self.world = World(online=online, verbose=verbose)
        self.title = cfg.get("title", "").strip() or "日本"
        self.header_title = cfg.get("header") or f"{self.title}のご当地ソング"
        self._scratch = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
        self._prepare()

    # ============================================================ 準備
    def _prepare(self):
        w = self.world
        songs = []
        # 1回目：場所を解決（近い候補を選ぶため、解決済みの点を目安にする）
        for i, s in enumerate(self.cfg["songs"]):
            places = s.get("places", [])
            if isinstance(places, str):
                places = [x for x in _split(places)]
            feats = []
            for p in places:
                if isinstance(p, str): p = {"name": p}
                f = w.resolve(p)
                if f.kind == "point" and w.hint is None: w.hint = f.label_pt
                feats.append(f)
            songs.append({**s, "feats": feats})
        self.songs = songs
        # 題名のエリア（湘南・津軽など）。イントロで輪郭を見せる
        focus = self.cfg.get("focus", self.title)
        self.focus = None
        if focus:
            fp = focus if isinstance(focus, dict) else {"name": focus, "type": "area"}
            ff = w.resolve(fp)
            if ff.kind == "area": self.focus = ff
        # 緯度補正
        allb = [b for s in songs for f in s["feats"] if (b := _bounds(f))]
        if self.focus: allb.append(_bounds(self.focus))
        if not allb:
            raise SystemExit("地図に出せる場所が1つもありません。places を確認してください。")
        self.all_bounds = _union(allb)
        lat0 = (self.all_bounds[1] + self.all_bounds[3]) / 2
        self.LATK = 1 / math.cos(math.radians(lat0))
        # 地名の数（チップに出る名前の重複なし）
        names = []
        for s in songs:
            for f in s["feats"]:
                if f.label not in names: names.append(f.label)
        self.n_places = len(names)
        # カードの高さとカメラ
        for i, s in enumerate(songs):
            s["card"] = self._card_layout(i, s)
            b = [x for f in s["feats"] if (x := _bounds(f))]
            if s.get("view"):  # 手動のカメラ [西, 南, 東, 北]
                b = [tuple(s["view"])]
            s["cam"] = self._fit(_union(b) if b else self.all_bounds, s["card"]["h"])
            s["sides"] = self._label_sides(s)
        self.cam_all = self._fit(self.all_bounds, 300)
        self.cam_outro = self._fit(self.all_bounds, 300)
        # 時間割
        t = self.opt["intro_sec"]
        for i, s in enumerate(songs):
            s["t0"] = t
            prev = self.cam_all if i == 0 else songs[i-1]["cam"]
            s["move"] = self.opt["move_sec"] * (1.5 if self._far(prev, s["cam"]) else 1.0)
            s["dur"] = float(s.get("sec") or self.opt["song_sec"])
            t += s["dur"]
        self.t_outro = t
        self.total = t + self.opt["outro_sec"]
        # 背景データ
        self._prep_base()
        self._base_key = None
        self._base_surf = None
        if self.verbose:
            self.report()

    def summary(self):
        """地名の解決結果（Web画面用）"""
        return {
            "header": self.header_title, "songs_n": len(self.songs), "places_n": self.n_places,
            "seconds": round(self.total, 1),
            "focus": f"{self.focus.name}（{self.focus.source}）" if self.focus else "",
            "songs": [{"title": s["title"], "artist": s.get("artist", ""),
                       "places": [{"label": f.label, "kind": f.kind, "source": f.source} for f in s["feats"]]}
                      for s in self.songs],
        }

    def report(self):
        print(f"■ {self.header_title}  {len(self.songs)}曲・{self.n_places}地名・{self.total:.1f}秒")
        if self.focus: print(f"  題名のエリア: {self.focus.name} ({self.focus.source})")
        for i, s in enumerate(self.songs, 1):
            print(f"  {i:02d}. {s['title']} / {s.get('artist','')}")
            for f in s["feats"]:
                mark = {"point": "●", "area": "▢", "line": "━", "none": "×"}[f.kind]
                print(f"      {mark} {f.label:<12} {f.kind:<5} {f.source}")

    # ============================================================ 投影・カメラ
    # cam = (中心経度, 中心緯度, 1度あたりpx, 中心を置く画面y)
    def project(self, cam, lon, lat):
        cx, cy, s, sy = cam
        return self.W/2 + (lon-cx)*s, sy - (lat-cy)*s*self.LATK

    def project_np(self, cam, arr):
        cx, cy, s, sy = cam
        return np.column_stack((self.W/2 + (arr[:, 0]-cx)*s, sy - (arr[:, 1]-cy)*s*self.LATK))

    def view_bounds(self, cam):
        cx, cy, s, sy = cam
        x0 = cx - (self.W/2)/s; x1 = cx + (self.W/2)/s
        y1 = cy + sy/(s*self.LATK); y0 = cy - (self.H-sy)/(s*self.LATK)
        return (x0, y0, x1, y1)

    def _fit(self, b, card_h):
        top = self.HEADER + 70
        bot = self.H - 90 - card_h - 70
        vh = max(300, bot - top); vw = self.W - 2*120
        lat = (b[1]+b[3])/2
        span = self.opt["point_span_km"] / (111.0 * math.cos(math.radians(lat)))
        dx = max(b[2]-b[0], span); dy = max(b[3]-b[1], span*0.6)
        s = min(vw/dx, vh/(dy*self.LATK))
        return ((b[0]+b[2])/2, (b[1]+b[3])/2, s, (top+bot)/2)

    def _far(self, a, b):
        s = min(a[2], b[2])
        return math.hypot((a[0]-b[0])*s, (a[1]-b[1])*s*self.LATK) > self.W*0.9

    def lerp_cam(self, a, b, t):
        e = ease(t)
        la, lb = math.log(a[2]), math.log(b[2])
        ls = la + (lb-la)*e
        # 遠くへ飛ぶときは一度引いてから寄る
        s_min = min(a[2], b[2])
        dist = math.hypot((a[0]-b[0])*s_min, (a[1]-b[1])*s_min*self.LATK)
        if dist > self.W*0.9:
            need = math.log(s_min * self.W*0.8 / dist)
            bump = max(0.0, min(la, lb) - need)
            ls -= bump * math.sin(math.pi*e)
        return (a[0]+(b[0]-a[0])*e, a[1]+(b[1]-a[1])*e, math.exp(ls), a[3]+(b[3]-a[3])*e)

    def cam_at(self, t):
        songs = self.songs
        if t < self.opt["intro_sec"]:
            return self.cam_all
        if t >= self.t_outro:
            return self.lerp_cam(songs[-1]["cam"], self.cam_outro, (t-self.t_outro)/1.4)
        for i, s in enumerate(songs):
            if s["t0"] <= t < s["t0"] + s["dur"]:
                prev = self.cam_all if i == 0 else songs[i-1]["cam"]
                return self.lerp_cam(prev, s["cam"], (t-s["t0"])/s["move"])
        return self.cam_all

    # ============================================================ 文字
    def font(self, ctx, size, bold=True):
        ctx.select_font_face(self.FONT, cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(size)

    def tw(self, ctx, s, size=None, bold=True):
        if size: self.font(ctx, size, bold)
        return ctx.text_extents(s).x_advance

    def text(self, ctx, s, x, y, size, color, bold=True, align="left", alpha=1.0, spacing=0):
        self.font(ctx, size, bold)
        w = (sum(ctx.text_extents(c).x_advance for c in s) + spacing*(len(s)-1)) if spacing else ctx.text_extents(s).x_advance
        if align == "center": x -= w/2
        elif align == "right": x -= w
        ctx.set_source_rgba(color[0], color[1], color[2], color[3]*alpha)
        if spacing:
            for c in s:
                ctx.move_to(x, y); ctx.show_text(c); x += ctx.text_extents(c).x_advance + spacing
        else:
            ctx.move_to(x, y); ctx.show_text(s)
        return w

    def wrap(self, ctx, s, size, maxw, bold=False):
        self.font(ctx, size, bold)
        lines = []
        for para in str(s).split("\n"):
            cur = ""
            for ch in para:
                if ctx.text_extents(cur+ch).x_advance > maxw and cur:
                    if ch in "。、」）！？・ー":
                        cur += ch; continue
                    lines.append(cur); cur = ch
                else:
                    cur += ch
            lines.append(cur)
        return [l for l in lines if l != ""] or []

    # ============================================================ 背景地図
    def _prep_base(self):
        w = self.world
        self.pref_rings = []   # (bbox, np.array)
        self.pref_label = []
        for name, (code, g) in w.prefs.items():
            for poly in _polys(g):
                arr = np.asarray(poly.exterior.coords)
                self.pref_rings.append((poly.bounds, arr, code))
            rp = max(_polys(g), key=lambda q: q.area).representative_point()
            self.pref_label.append((name, rp.x, rp.y))
        self.pref_bbox = {code: g.bounds for name, (code, g) in w.prefs.items()}
        self._muni_rings = {}
        self.rail_paths = [(r["path"], rid) for rid, r in w.rail.items() if not rid.startswith("Base.")]
        self.rail_np = [np.asarray(p) for p, _ in self.rail_paths]

    def muni_rings(self, code):
        if code not in self._muni_rings:
            rings = []
            for keys, g in self.world.muni_features(code):
                for poly in _polys(g):
                    rings.append((poly.bounds, np.asarray(poly.exterior.coords)))
            self._muni_rings[code] = rings
        return self._muni_rings[code]

    def _path(self, ctx, cam, arr, close=True):
        pts = self.project_np(cam, arr)
        ctx.move_to(*pts[0])
        for x, y in pts[1:]: ctx.line_to(x, y)
        if close: ctx.close_path()

    def draw_base(self, ctx, cam):
        key = tuple(round(v, 7) for v in cam)
        if key == self._base_key:
            ctx.set_source_surface(self._base_surf, 0, 0); ctx.paint(); return
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        c = cairo.Context(surf)
        self._draw_base(c, cam)
        self._base_key, self._base_surf = key, surf
        ctx.set_source_surface(surf, 0, 0); ctx.paint()

    def _draw_base(self, ctx, cam):
        vb = self.view_bounds(cam)
        vw = vb[2] - vb[0]
        ctx.set_source_rgba(*BG); ctx.paint()
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        vis = [r for r in self.pref_rings if _hit(r[0], vb)]
        for bb, arr, code in vis:
            self._path(ctx, cam, arr)
        ctx.set_source_rgba(*LAND); ctx.fill()
        detail = vw < 2.2
        if detail:
            codes = [c for c, bb in self.pref_bbox.items() if _hit(bb, vb)]
            ctx.set_line_width(1.3 if vw > 0.6 else 1.8)
            for code in codes:
                for bb, arr in self.muni_rings(code):
                    if _hit(bb, vb): self._path(ctx, cam, arr)
            ctx.set_source_rgba(*BORDER); ctx.stroke()
        for bb, arr, code in vis:
            self._path(ctx, cam, arr)
        ctx.set_source_rgba(*(PREFBORDER if detail else BORDER)); ctx.set_line_width(2.2 if detail else 1.4); ctx.stroke()
        # 首都圏の鉄道（うすく）
        if vw < 1.3 and _hit((138.9, 34.9, 140.9, 36.4), vb):
            ctx.set_line_width(1.6 if vw > 0.4 else 2.2)
            for (path, rid), arr in zip(self.rail_paths, self.rail_np):
                self._path(ctx, cam, arr, close=False)
            ctx.set_source_rgba(*BGRAIL); ctx.stroke()
        # 地名ラベル
        placed = []
        if detail and vw < 0.9:
            idx = self.world.muni_index
            for name, cands in idx.items():
                for m in cands:
                    if "市" in name[:-1] and name.endswith("区") and not name.startswith("東京"):
                        continue  # 政令市の区は省く
                    bb = m["bb"]
                    if not _hit(bb, vb): continue
                    pw = (bb[2]-bb[0]) * cam[2]
                    if pw < 150: continue
                    x, y = self.project(cam, *m["c"])
                    if not (70 < x < self.W-70 and self.HEADER+50 < y < self.H-40): continue
                    size = 26 if vw > 0.35 else 30
                    w_ = self.tw(ctx, name, size, False)
                    r = (x-w_/2-6, y-size, x+w_/2+6, y+8)
                    if any(_overlap(r, q) for q in placed): continue
                    placed.append(r)
                    self.text(ctx, name, x, y, size, GREY, bold=False, align="center", alpha=0.55)
        elif not detail:
            for name, x, y in self.pref_label:
                if not (vb[0] < x < vb[2] and vb[1] < y < vb[3]): continue
                px, py = self.project(cam, x, y)
                size = 24 if vw > 8 else 28
                n = name if name == "北海道" else name[:-1]
                w_ = self.tw(ctx, n, size, False)
                r = (px-w_/2-6, py-size, px+w_/2+6, py+8)
                if any(_overlap(r, q) for q in placed): continue
                placed.append(r)
                self.text(ctx, n, px, py, size, GREY, bold=False, align="center", alpha=0.5)

    # ============================================================ 地物
    def draw_area(self, ctx, cam, f, a=1.0, faint=False, label=True):
        for poly in _polys(f.geom):
            for ring in [poly.exterior] + list(poly.interiors):
                self._path(ctx, cam, np.asarray(ring.coords))
        ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        ctx.set_source_rgba(TEAL[0], TEAL[1], TEAL[2], (0.03 if faint else 0.20)*a); ctx.fill_preserve()
        ctx.set_source_rgba(TEAL[0], TEAL[1], TEAL[2], (0.35 if faint else 0.95)*a)
        ctx.set_line_width(2 if faint else 3.5); ctx.set_dash([10, 7] if faint else []); ctx.stroke()
        ctx.set_dash([])
        if label and not faint:
            b = f.geom.bounds
            pw = (b[2]-b[0])*cam[2]
            n = f.label
            size = max(38, min(72, pw / max(2, len(n)) / 1.8))
            x, y = self.project(cam, *getattr(f, "draw_lp", f.label_pt))
            x = min(max(x, 140), self.W-140)
            self.text(ctx, n, x, y+size*0.35, size, TEAL, align="center", alpha=0.9*a, spacing=size*0.25)

    def draw_line(self, ctx, cam, f, prog=1.0, a=1.0, faint=False):
        style = getattr(f, "style", "rail")
        for path in f.paths:
            arr = np.asarray(path)
            pts = self.project_np(cam, arr)
            if prog < 1.0:
                pts = _cut(pts, prog)
            if len(pts) < 2: continue
            def stroke(width, col, alpha, dash=None):
                ctx.move_to(*pts[0])
                for x, y in pts[1:]: ctx.line_to(x, y)
                ctx.set_source_rgba(col[0], col[1], col[2], alpha*a)
                ctx.set_line_width(width); ctx.set_dash(dash or [])
                ctx.stroke(); ctx.set_dash([])
            ctx.set_line_cap(cairo.LINE_CAP_ROUND); ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            if faint:
                stroke(3, ORANGE if style == "road" else RIVER if style == "river" else RAILC, 0.35)
                continue
            if style == "road":
                stroke(22, ORANGE, 0.22); stroke(9, ORANGE, 1.0)
            elif style == "river":
                stroke(18, RIVER, 0.25); stroke(7, RIVER, 1.0)
            else:   # 鉄道：白黒のしましま
                stroke(20, TEAL, 0.18); stroke(11, DARK, 1.0); stroke(7, RAILC, 1.0)
                ctx.set_line_cap(cairo.LINE_CAP_BUTT); stroke(7, DARK, 1.0, [16, 16])
            # 先頭の光る点
            if prog < 1.0:
                x, y = pts[-1]
                col = ORANGE if style == "road" else RIVER if style == "river" else TEAL
                ctx.set_source_rgba(col[0], col[1], col[2], 0.35*a); ctx.arc(x, y, 18, 0, 2*math.pi); ctx.fill()
                ctx.set_source_rgba(*WHITE[:3], a); ctx.arc(x, y, 8, 0, 2*math.pi); ctx.fill()

    def line_label(self, ctx, cam, f, a=1.0):
        x, y = self.project(cam, *f.label_pt)
        x = min(max(x, 160), self.W-160)
        style = getattr(f, "style", "rail")
        col = ORANGE if style == "road" else RIVER if style == "river" else TEAL
        self.font(ctx, 32, True)
        w = ctx.text_extents(f.label).x_advance + 30; h = 52
        bx, by = x - w/2, y - h - 22
        _rrect(ctx, bx, by, w, h, 26); ctx.set_source_rgba(*DARK[:3], 0.92*a); ctx.fill_preserve()
        ctx.set_source_rgba(col[0], col[1], col[2], a); ctx.set_line_width(2.5); ctx.stroke()
        self.text(ctx, f.label, bx+15, by+37, 32, WHITE, alpha=a)

    def pin_box(self, ctx, cam, f, side):
        x, y = self.project(cam, *f.label_pt)
        self.font(ctx, 32, True)
        bw = ctx.text_extents(f.label).x_advance + 26; bh = 50; r = 13
        if side == "R": bx, by = x + r + 12, y - bh/2
        elif side == "L": bx, by = x - r - 12 - bw, y - bh/2
        elif side == "T": bx, by = x - bw/2, y - r - 14 - bh
        elif side == "B": bx, by = x - bw/2, y + r + 14
        elif side == "TR": bx, by = x + r, y - r - 6 - bh
        elif side == "TL": bx, by = x - r - bw, y - r - 6 - bh
        elif side == "BR": bx, by = x + r, y + r + 6
        else: bx, by = x - r - bw, y + r + 6
        return bx, by, bw, bh

    def draw_pin(self, ctx, cam, f, side="R", a=1.0, scale=1.0, label=True):
        x, y = self.project(cam, *f.label_pt)
        r = 13*scale
        ctx.set_source_rgba(TEAL[0], TEAL[1], TEAL[2], 0.25*a); ctx.arc(x, y, r*2, 0, 2*math.pi); ctx.fill()
        ctx.set_source_rgba(*DARK[:3], a); ctx.arc(x, y, r+4, 0, 2*math.pi); ctx.fill()
        ctx.set_source_rgba(TEAL[0], TEAL[1], TEAL[2], a); ctx.arc(x, y, r, 0, 2*math.pi); ctx.fill()
        if not label: return
        bx, by, bw, bh = self.pin_box(ctx, cam, f, side)
        _rrect(ctx, bx, by, bw, bh, 8); ctx.set_source_rgba(0.047, 0.063, 0.078, 0.92*a); ctx.fill_preserve()
        ctx.set_source_rgba(*WHITE[:3], 0.85*a); ctx.set_line_width(2); ctx.stroke()
        self.text(ctx, f.label, bx+13, by+36, 32, WHITE, alpha=a)

    def _label_sides(self, s):
        """ピンのラベルを左右上下のどこに置くか（重なりと画面外を避ける）"""
        cam = s["cam"]; ctx = self._scratch
        placed, sides = [], {}
        bottom = self.H - 90 - s["card"]["h"] - 10
        has_pt = any(f.kind == "point" for f in s["feats"])
        for f in s["feats"]:   # 先にすべてのピンとエリア名の場所を押さえる
            if f.kind == "point":
                x, y = self.project(cam, *f.label_pt); placed.append((x-18, y-18, x+18, y+18))
            elif f.kind == "area":
                if has_pt:   # ピンと重ならないよう、エリア名はエリアの上に出す
                    b = _bounds(f)
                    x, y = self.project(cam, (b[0]+b[2])/2, b[3])
                    y = max(y - 20, self.HEADER + 90)
                    f.draw_lp = ((b[0]+b[2])/2, cam[1] + (cam[3]-y)/(cam[2]*self.LATK))
                x, y = self.project(cam, *getattr(f, "draw_lp", f.label_pt))
                placed.append((x-120, y-50, x+120, y+20))
        for i, f in enumerate(s["feats"]):
            if f.kind != "point": continue
            if getattr(f, "side", None):
                sides[i] = f.side
            else:
                x, y = self.project(cam, *f.label_pt)
                order = ["R", "L", "T", "B", "TR", "TL", "BR", "BL"] if x < self.W*0.62 else \
                        ["L", "R", "T", "B", "TL", "TR", "BL", "BR"]
                best, best_score = order[0], 1e18
                for k, sd in enumerate(order):
                    bx, by, bw, bh = self.pin_box(ctx, cam, f, sd)
                    r = (bx, by, bx+bw, by+bh)
                    score = sum(_area_ov(r, q) for q in placed) * 10 + k
                    if not (r[0] > 16 and r[2] < self.W-16 and r[1] > self.HEADER+10 and r[3] < bottom):
                        score += 1e6
                    if score < best_score: best, best_score = sd, score
                sides[i] = best
            bx, by, bw, bh = self.pin_box(ctx, cam, f, sides[i])
            placed.append((bx, by, bx+bw, by+bh))
        return sides

    # ============================================================ カード
    def _card_layout(self, i, s):
        ctx = self._scratch
        x, w, pad = 44, self.W-88, 40
        maxw = w - pad*2
        title = "♪ " + s["title"]
        ts = 64
        while ts > 40 and self.tw(ctx, title, ts) > maxw: ts -= 2
        tlines = [title] if self.tw(ctx, title, ts) <= maxw else self.wrap(ctx, title, ts, maxw, True)[:2]
        n_map = len(s["feats"])
        kind = s.get("kind") or "曲名・歌詞に地名"
        kind_label = f"{kind}・{n_map}か所" if n_map else kind
        chips = [f.label for f in s["feats"]] + list(s.get("extra_chips", []))
        if len(chips) > 14:   # 地名が多い曲はチップを省略
            chips = chips[:13] + [f"ほか{len(chips) - 13}か所"]
        self.font(ctx, 28, True)
        rows, cur, cw = [], [], 0
        for c in chips:
            cwid = ctx.text_extents(c).x_advance + 32
            if cw + cwid > maxw and cur: rows.append(cur); cur, cw = [], 0
            cur.append((c, cwid)); cw += cwid + 12
        if cur: rows.append(cur)
        note = self.wrap(ctx, s.get("note", ""), 32, maxw) if s.get("note") else []
        src = self.wrap(ctx, "出典: " + s["source"], 24, maxw) if s.get("source") else []
        h = pad + 30 + 22 + len(tlines)*(ts+12) + 40 + 22 + len(rows)*56
        h += (14 + len(note)*46) if note else 0
        h += (10 + len(src)*34) if src else 0
        h += pad - 6
        return dict(h=h, ts=ts, tlines=tlines, kind=kind_label, rows=rows, note=note, src=src)

    def draw_card(self, ctx, i, s, a=1.0, dy=0):
        L = s["card"]
        x, w, pad = 44, self.W-88, 40
        y = self.H - 90 - L["h"] + dy
        ctx.push_group()
        _rrect(ctx, x, y, w, L["h"], 26); ctx.set_source_rgba(*CARD[:3], 0.96); ctx.fill_preserve()
        ctx.set_source_rgba(*CARDLINE); ctx.set_line_width(2); ctx.stroke()
        cy = y + pad + 26
        nw = self.text(ctx, f"No.{i+1:02d}", x+pad, cy, 28, TEAL)
        self.text(ctx, L["kind"], x+pad+nw+22, cy, 26, TEAL, bold=False)
        cy += 22
        for tl in L["tlines"]:
            cy += L["ts"] + 6
            self.text(ctx, tl, x+pad-4, cy, L["ts"], WHITE); cy += 6
        cy += 40
        self.text(ctx, s.get("artist", ""), x+pad, cy, 32, GREY, bold=False)
        cy += 22
        for row in L["rows"]:
            cx = x + pad
            for c, cwid in row:
                _rrect(ctx, cx, cy, cwid, 44, 22); ctx.set_source_rgba(*CHIP); ctx.fill()
                self.text(ctx, c, cx+16, cy+32, 28, WHITE)
                cx += cwid + 12
            cy += 56
        if L["note"]:
            cy += 14 + 32
            for ln in L["note"]:
                self.text(ctx, ln, x+pad, cy, 32, NOTE, bold=False); cy += 46
            cy -= 32
        if L["src"]:
            cy += 10 + 26
            for ln in L["src"]:
                self.text(ctx, ln, x+pad, cy, 24, GREY, bold=False); cy += 34
        ctx.pop_group_to_source(); ctx.paint_with_alpha(a)

    def draw_center_card(self, ctx, big, accent, l1, l2="", a=1.0):
        W, H = self.W, self.H
        lines2 = self.wrap(ctx, l2, 22, W-240) if l2 else []
        h = 300 + len(lines2)*30
        x, y, w = 80, H - 90 - h, W - 160
        ctx.push_group()
        _rrect(ctx, x, y, w, h, 28); ctx.set_source_rgba(*CARD[:3], 0.96); ctx.fill_preserve()
        ctx.set_source_rgba(*CARDLINE); ctx.set_line_width(2); ctx.stroke()
        bs = 52
        while bs > 34 and self.tw(ctx, big, bs) > w-60: bs -= 2
        self.text(ctx, big, W/2, y+92, bs, WHITE, align="center")
        self.text(ctx, accent, W/2, y+172, 76, TEAL, align="center")
        self.text(ctx, l1, W/2, y+240, 30, GREY, bold=False, align="center")
        yy = y + 285
        for ln in lines2:
            self.text(ctx, ln, W/2, yy, 22, GREY, bold=False, align="center", alpha=0.8); yy += 30
        ctx.pop_group_to_source(); ctx.paint_with_alpha(a)

    def draw_header(self, ctx, sub):
        W = self.W
        ctx.set_source_rgba(*HEADBG[:3], 0.96); ctx.rectangle(0, 0, W, self.HEADER); ctx.fill()
        size = 54
        while size > 32 and self.tw(ctx, self.header_title, size) > W-96: size -= 2
        self.text(ctx, self.header_title, 48, 84, size, WHITE)
        self.text(ctx, sub, 48, 134, 28, GREY, bold=False)
        ctx.set_source_rgba(*TEAL); ctx.rectangle(0, self.HEADER-3, W, 3); ctx.fill()

    def credits(self):
        parts = ["地図: 国土数値情報（行政区域）"]
        u = self.world.used_sources
        if "mt3d" in u: parts.append("路線: Mini Tokyo 3D")
        if "gsi" in u: parts.append("位置: 国土地理院")
        if "osm" in u: parts.append("© OpenStreetMap contributors")
        extra = self.cfg.get("credit")
        if extra: parts.append(extra)
        return " / ".join(parts)

    # ============================================================ 1コマ
    def draw_feat_faint(self, ctx, cam, f, a=1.0):
        if f.kind == "area": self.draw_area(ctx, cam, f, a=a, faint=True, label=False)
        elif f.kind == "line": self.draw_line(ctx, cam, f, a=a, faint=True)
        elif f.kind == "point": self.draw_pin(ctx, cam, f, a=0.4*a, scale=0.55, label=False)

    def render(self, t, ctx):
        cam = self.cam_at(t)
        self.draw_base(ctx, cam)
        songs = self.songs
        M = len(songs)
        intro = self.opt["intro_sec"]
        if t < intro:
            if self.focus:
                fa = ease((t-0.6)/0.8)
                if fa > 0: self.draw_area(ctx, cam, self.focus, a=fa)
            a = ease((t-0.3)/0.6) * (1 - ease((t-intro+0.4)/0.4))
            self.draw_center_card(ctx, f"{self.title}の", "ご当地ソング", f"{M}曲を1曲ずつめぐります", a=a)
            self.draw_header(ctx, f"全{M}曲" + (f"・{self.opt['header_note']}" if self.opt["header_note"] else ""))
            return
        if t < self.t_outro:
            k = max(i for i, s in enumerate(songs) if s["t0"] <= t)
            s = songs[k]; lt = t - s["t0"]; mv = s["move"]
            # これまでの曲はうすく残す
            for j in range(k):
                for f in songs[j]["feats"]:
                    self.draw_feat_faint(ctx, cam, f)
            # 今の曲：エリア → 線 → 点 の順に出す
            n_line = 0
            for f in s["feats"]:
                if f.kind == "area":
                    aa = ease((lt - mv*0.6)/0.6)
                    if aa > 0: self.draw_area(ctx, cam, f, a=aa)
            line_t0 = mv*0.75
            for f in s["feats"]:
                if f.kind == "line":
                    pr = ease((lt - line_t0 - n_line*0.3)/1.6)
                    if pr > 0:
                        self.draw_line(ctx, cam, f, prog=pr)
                        la = ease((pr-0.55)/0.3)
                        if la > 0: self.line_label(ctx, cam, f, a=la)
                    n_line += 1
            pin_t0 = mv*0.8 + (0.9 if n_line else 0)
            n_pt = sum(1 for f in s["feats"] if f.kind == "point")
            dense = n_pt > 12          # 地名が多い曲：次々にピンを打ち、ラベルは出た瞬間だけ見せる
            step = max(0.05, min(0.22, (s["dur"] - pin_t0 - 1.2) / max(1, n_pt))) if dense else 0.22
            pi = 0
            for idx, f in enumerate(s["feats"]):
                if f.kind != "point": continue
                st = pin_t0 + pi*step
                pa = ease((lt - st)/0.3)
                if pa > 0:
                    if dense:
                        la = 1 - ease((lt - st - 0.7)/0.3)
                        self.draw_pin(ctx, cam, f, s["sides"].get(idx, "R"), a=pa, scale=0.45+0.3*pa, label=False)
                        if la > 0:
                            self.draw_pin(ctx, cam, f, s["sides"].get(idx, "R"), a=pa*la, scale=0.75, label=True)
                    else:
                        self.draw_pin(ctx, cam, f, s["sides"].get(idx, "R"), a=pa, scale=0.6+0.4*pa)
                pi += 1
            ca = ease((lt-0.45)/0.5) * (1 - ease((lt - s["dur"] + 0.35)/0.35))
            self.draw_card(ctx, k, s, a=ca, dy=(1-ease((lt-0.45)/0.5))*60)
            self.draw_header(ctx, f"{k+1:02d} / {M}曲")
            return
        lt = t - self.t_outro
        for j, s in enumerate(songs):
            for f in s["feats"]:
                aa = ease((lt-0.5-j*0.1)/0.4)
                if aa <= 0: continue
                if f.kind == "line": self.draw_line(ctx, cam, f, a=aa*0.8)
                elif f.kind == "area": self.draw_area(ctx, cam, f, a=aa*0.6, label=False)
        for j, s in enumerate(songs):
            for f in s["feats"]:
                if f.kind == "point":
                    self.draw_pin(ctx, cam, f, a=ease((lt-0.5-j*0.1)/0.3), scale=0.75, label=False)
        a = ease((lt-1.3)/0.5)
        self.draw_center_card(ctx, f"{self.title}のご当地ソング", f"{M}曲・{self.n_places}地名",
                              "曲名・歌詞・モチーフに出てくる場所", self.credits(), a=a)
        self.draw_header(ctx, f"{M}曲")

    # ============================================================ 出力
    def write(self, out, preview=None, on_progress=None):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        ctx = cairo.Context(surf)
        if preview is not None:
            stem = os.path.splitext(out)[0]
            paths = []
            for ts in preview:
                self.render(float(ts), ctx); p = f"{stem}_{float(ts):05.1f}s.png"
                surf.write_to_png(p); paths.append(p)
            return paths
        n = int(self.total * self.FPS)
        p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra",
                              "-s", f"{self.W}x{self.H}", "-r", str(self.FPS), "-i", "-",
                              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
                              "-movflags", "+faststart", out], stdin=subprocess.PIPE)
        for fr in range(n):
            self.render(fr / self.FPS, ctx); surf.flush()
            p.stdin.write(bytes(surf.get_data()))
            if on_progress and fr % self.FPS == 0:
                on_progress(fr / n)
            if self.verbose and fr % (self.FPS*5) == 0:
                print(f"  {fr/self.FPS:5.1f} / {self.total:.1f} 秒", flush=True)
        p.stdin.close(); p.wait()
        if p.returncode != 0:
            raise RuntimeError("ffmpeg が失敗しました（ffmpeg が入っているか確認してください）")
        if on_progress: on_progress(1.0)
        return out


# ------------------------------------------------------------------ 補助
def _split(s):
    """「・」で分ける（かっこの中は分けない）。かっこ書きや「ほか」「など」は外す"""
    import re
    out, cur, depth = [], "", 0
    for ch in s:
        if ch in "（(": depth += 1
        if ch in "）)": depth = max(0, depth - 1)
        if depth == 0 and ch in "・、,，/／":
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    res = []
    for x in out:
        x = re.sub(r"[（(].*?[）)]", "", x)
        x = re.sub(r"\s*(ほか|など)$", "", x).strip()
        if x: res.append(x)
    return res

def _polys(g):
    if g is None: return []
    if g.geom_type == "Polygon": return [g]
    if g.geom_type == "MultiPolygon": return list(g.geoms)
    return [q for q in getattr(g, "geoms", []) if q.geom_type == "Polygon"]

def _bounds(f):
    if f is None or f.kind == "none": return None
    if f.kind == "area":
        ps = _polys(f.geom)
        big = max(q.area for q in ps)
        ps = [q for q in ps if q.area >= big*0.06]   # 小さな離島はカメラ合わせから外す
        bs = [q.bounds for q in ps]
        return _union(bs)
    return f.bounds()

def _union(bs):
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

def _hit(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])

def _overlap(a, b):
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])

def _area_ov(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0]); h = min(a[3], b[3]) - max(a[1], b[1])
    return w*h if w > 0 and h > 0 else 0

def _rrect(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x+w-r, y+r, r, -math.pi/2, 0); ctx.arc(x+w-r, y+h-r, r, 0, math.pi/2)
    ctx.arc(x+r, y+h-r, r, math.pi/2, math.pi); ctx.arc(x+r, y+r, r, math.pi, 1.5*math.pi)
    ctx.close_path()

def _cut(pts, t):
    """画面上の折れ線を長さの割合 t までで切る（ビーっと引く用）"""
    d = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    L = np.concatenate([[0], np.cumsum(d)])
    target = L[-1] * t
    i = int(np.searchsorted(L, target))
    if i <= 0: return pts[:1]
    if i >= len(pts): return pts
    u = (target - L[i-1]) / (L[i]-L[i-1] or 1)
    end = pts[i-1] + (pts[i]-pts[i-1])*u
    return np.vstack([pts[:i], end])
