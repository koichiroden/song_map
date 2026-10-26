"""地名 → 地図上の形（点・エリア・線）に変換する。

解決の順番
  点   : 設定の lonlat → landmarks.json → 市区町村 → 首都圏の駅 → 都道府県 → 地理院の地名検索 → Nominatim
  エリア: 設定の munis/pref/box → areas.json → 都道府県 → 市区町村の形 → (見つからなければ点)
  線   : 設定の coords/mt3d/stations → lines.json → Mini Tokyo 3D の路線名 → (見つからなければ点)
ネット検索の結果は cache/geocode.json に保存し、2回目からは使い回す。
"""
import json, os, re, sys, time, math
from shapely.geometry import shape, box as sbox, Point
from shapely.ops import unary_union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "cache")

ROAD_RE = re.compile(r"国道|号|道路|ライン|ウェイ|ウエイ|街道|通り|ルート|Route|ROUTE|R\d|246|京浜|首都高|湾岸|中央道|高速|バイパス|JCT")
LINE_RE = re.compile(r"線$|線沿線$|電鉄|電車|新幹線|鉄道|ライン$|国道|号線?$|街道|フリーウェイ|ルート|Route|ROUTE|246|京浜|首都高|高速")


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


class Feature:
    """地図に描くもの1つ。kind = point / area / line / none（地図に出さない）"""

    def __init__(self, name, kind, geom=None, label_pt=None, paths=None, source=""):
        self.name = name
        self.kind = kind
        self.geom = geom            # shapely（point: Point, area: Polygon/MultiPolygon）
        self.paths = paths or []    # line: [[(lon,lat), ...], ...]
        self.label_pt = label_pt    # (lon, lat)
        self.style = "rail"
        self.source = source        # どこで見つけたか（確認用）
        self.label = name

    def bounds(self):
        if self.kind == "line":
            xs = [p[0] for path in self.paths for p in path]
            ys = [p[1] for path in self.paths for p in path]
            return (min(xs), min(ys), max(xs), max(ys))
        if self.geom is not None:
            return self.geom.bounds
        return None

    def __repr__(self):
        return f"<{self.kind} {self.name} ({self.source})>"


class World:
    def __init__(self, online=True, verbose=True):
        self.online = online
        self.verbose = verbose
        self.areas = _load("areas.json")
        self.lines = _load("lines.json")
        self.landmarks = _load("landmarks.json")
        self.aliases = _load("aliases.json") if os.path.exists(os.path.join(DATA, "aliases.json")) else {}
        self.ignore = set(_load("ignore.json")) if os.path.exists(os.path.join(DATA, "ignore.json")) else set()
        self.rail = _load("kanto_rail.json")
        self.stations = _load("kanto_stations.json")
        self.muni_index = _load("muni_index.json")
        pref = _load("prefectures.json")
        self.prefs = {}            # 都道府県名 → (コード, 形)
        for i, f in enumerate(pref["features"], start=1):
            self.prefs[f["properties"]["name"]] = (i, shape(f["geometry"]))
        self.pref_by_code = {c: (n, g) for n, (c, g) in self.prefs.items()}
        self._muni_cache = {}
        os.makedirs(CACHE, exist_ok=True)
        self._geo_path = os.path.join(CACHE, "geocode.json")
        self._geo = json.load(open(self._geo_path, encoding="utf-8")) if os.path.exists(self._geo_path) else {}
        self.used_sources = set()
        self.hint = None           # 近くを優先するための目安 (lon, lat)

    # ------------------------------------------------------------ 基本データ
    def muni_features(self, pref_code):
        """都道府県の市区町村ポリゴン [(キー一覧, 形)]"""
        if pref_code not in self._muni_cache:
            p = os.path.join(DATA, "muni", f"N03-21_{pref_code:02d}_210101.json")
            g = json.load(open(p, encoding="utf-8"))
            out = []
            for ft in g["features"]:
                pr = ft["properties"]
                city, ward = pr.get("N03_003"), pr.get("N03_004")
                keys = set()
                if ward: keys.add(ward)
                if city: keys.add(city)
                if city and ward: keys.add(city + ward)
                out.append((keys, shape(ft["geometry"])))
            self._muni_cache[pref_code] = out
        return self._muni_cache[pref_code]

    def pref_name(self, name):
        if name in self.prefs: return name
        for suf in ("都", "府", "県"):
            if name + suf in self.prefs: return name + suf
        return None

    def _same(self, table, name):
        seen = 0
        while name in table and isinstance(table[name], dict) and "same" in table[name] and seen < 5:
            name = table[name]["same"]; seen += 1
        return table.get(name), name

    def _pick(self, cands, pref=None):
        """候補が複数あるとき：指定の都道府県 → 目安の地点に近いもの → 先頭"""
        if pref:
            c2 = [c for c in cands if c.get("pref") == pref]
            if c2: cands = c2
        if self.hint and len(cands) > 1:
            hx, hy = self.hint
            cands = sorted(cands, key=lambda c: (c["c"][0]-hx)**2 + (c["c"][1]-hy)**2)
        return cands[0]

    def muni_lookup(self, name, pref=None):
        for k in (name, name + "市", name + "区", name + "町", name + "村"):
            if k in self.muni_index:
                return k, self._pick(self.muni_index[k], pref)
        return None, None

    # ------------------------------------------------------------ ネット検索
    def geocode(self, name):
        if name in self._geo:
            return self._geo[name]
        if not self.online:
            return None
        import requests
        res = None
        try:
            r = requests.get("https://msearch.gsi.go.jp/address-search/AddressSearch",
                             params={"q": name}, timeout=10)
            js = r.json()
            if js:
                # 名前が完全に一致するものを優先
                hit = next((j for j in js if j["properties"].get("title") == name), js[0])
                lon, lat = hit["geometry"]["coordinates"][:2]
                res = {"c": [lon, lat], "by": "gsi", "title": hit["properties"].get("title")}
        except Exception as e:
            if self.verbose: print(f"  地理院の検索に失敗: {name} ({e})", file=sys.stderr)
        if res is None:
            try:
                time.sleep(1.0)  # Nominatim の利用ルール（1秒1回）
                r = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": name, "format": "json", "limit": 1, "countrycodes": "jp",
                                         "accept-language": "ja"},
                                 headers={"User-Agent": "gotouchi-song-video/1.0"}, timeout=15)
                js = r.json()
                if js:
                    res = {"c": [float(js[0]["lon"]), float(js[0]["lat"])], "by": "osm",
                           "title": js[0].get("display_name", "")[:60]}
            except Exception as e:
                if self.verbose: print(f"  Nominatim の検索に失敗: {name} ({e})", file=sys.stderr)
        if res:
            self._geo[name] = res
            with open(self._geo_path, "w", encoding="utf-8") as f:
                json.dump(self._geo, f, ensure_ascii=False, indent=1)
        return res

    # ------------------------------------------------------------ 種類の推測
    def guess_type(self, name):
        name = self.aliases.get(name, name)
        if name in self.ignore:
            return "none"
        if name in self.lines or LINE_RE.search(name):
            return "line"
        if name in self.areas or self.pref_name(name):
            return "area"
        return "point"

    # ------------------------------------------------------------ 点
    def resolve_point(self, name, p):
        if p.get("lonlat"):
            x, y = p["lonlat"]; return Feature(name, "point", Point(x, y), (x, y), source="指定座標")
        if name in self.landmarks:
            x, y = self.landmarks[name]; return Feature(name, "point", Point(x, y), (x, y), source="landmarks")
        st = name[:-1] if name.endswith("駅") else name
        if st in self.stations and not p.get("pref"):
            x, y = self.stations[st]; self.used_sources.add("mt3d")
            return Feature(name, "point", Point(x, y), (x, y), source="首都圏の駅")
        k, m = self.muni_lookup(name, p.get("pref"))
        if m:
            x, y = m["c"]; return Feature(name, "point", Point(x, y), (x, y), source=f"市区町村:{k}")
        pn = self.pref_name(name)
        if pn:
            c = self.prefs[pn][1].representative_point()
            return Feature(name, "point", c, (c.x, c.y), source="都道府県")
        g = self.geocode(p.get("query") or name)
        if g:
            x, y = g["c"]; self.used_sources.add(g["by"])
            return Feature(name, "point", Point(x, y), (x, y), source=f"検索:{g['by']}:{g.get('title','')}")
        return None

    # ------------------------------------------------------------ エリア
    def _area_from_spec(self, name, spec, src):
        if "box" in spec:
            g = sbox(*spec["box"])
            return Feature(name, "area", g, tuple(g.centroid.coords[0]), source=src + ":box")
        geoms = []
        if "prefs" in spec:
            for code in spec["prefs"]:
                geoms.append(self.pref_by_code[code][1])
        if "munis" in spec:
            pc = spec.get("pref")
            want = set(spec["munis"])
            codes = [pc] if pc else range(1, 48)
            found = set()
            for c in codes:
                for keys, g in self.muni_features(c):
                    hit = keys & want
                    if hit:
                        geoms.append(g); found |= hit
                if found and not pc: break
            miss = want - found
            if miss and self.verbose:
                print(f"  ※ {name}: 見つからない市町村 {sorted(miss)}", file=sys.stderr)
        if not geoms:
            return None
        g = unary_union(geoms).buffer(0.0008).buffer(-0.0008)
        big = max(g.geoms, key=lambda q: q.area) if g.geom_type == "MultiPolygon" else g
        lp = big.representative_point()
        if spec.get("label_at"): lp = Point(*spec["label_at"])
        return Feature(name, "area", g, (lp.x, lp.y), source=src)

    def resolve_area(self, name, p):
        if any(k in p for k in ("munis", "prefs", "box")):
            spec = {k: p[k] for k in ("munis", "pref", "prefs", "box", "label_at") if k in p}
            return self._area_from_spec(name, spec, "指定")
        spec, real = self._same(self.areas, name)
        if spec:
            return self._area_from_spec(name, spec, f"areas:{real}")
        pn = self.pref_name(name)
        if pn:
            return self._area_from_spec(name, {"prefs": [self.prefs[pn][0]]}, "都道府県")
        k, m = self.muni_lookup(name, p.get("pref"))
        if m:
            return self._area_from_spec(name, {"munis": [k], "pref": m["pref"]}, f"市区町村:{k}")
        return None

    # ------------------------------------------------------------ 線
    def _station_coord(self, s):
        if s in self.landmarks: return self.landmarks[s]
        g = self.geocode(s + "駅")
        if g: self.used_sources.add(g["by"]); return g["c"]
        if s in self.stations: return self.stations[s]   # 首都圏の駅（同名駅に注意）
        return None

    def _line_from_spec(self, name, spec, src):
        paths = []
        if "coords" in spec:
            paths.append([tuple(q) for q in spec["coords"]])
        if "mt3d" in spec:
            for rid in spec["mt3d"]:
                if rid in self.rail:
                    paths.append([tuple(q) for q in self.rail[rid]["path"]]); self.used_sources.add("mt3d")
                elif self.verbose:
                    print(f"  ※ {name}: Mini Tokyo 3D に {rid} がない", file=sys.stderr)
        if "stations" in spec:
            pts = []
            for s in spec["stations"]:
                c = self._station_coord(s)
                if c: pts.append(tuple(c))
                elif self.verbose: print(f"  ※ {name}: 駅 {s} の場所が分からない", file=sys.stderr)
            if spec.get("closed") and pts: pts.append(pts[0])
            if len(pts) >= 2: paths.append(pts)
        if not paths:
            return None
        style = spec.get("style") or ("river" if re.search(r"川$|河$", name) else
                                      "road" if ROAD_RE.search(name) else "rail")
        f = Feature(name, "line", paths=paths, source=src)
        f.style = style
        mid = _point_along(paths[0], 0.5)
        f.label_pt = mid
        return f

    def resolve_line(self, name, p):
        f = self._resolve_line(name, p)
        if f is not None and p.get("reverse"):   # 線を引く向きを逆にする
            f.paths = [list(reversed(q)) for q in f.paths][::-1]
        return f

    def _resolve_line(self, name, p):
        if any(k in p for k in ("coords", "mt3d", "stations")):
            spec = {k: p[k] for k in ("coords", "mt3d", "stations", "closed", "style") if k in p}
            return self._line_from_spec(name, spec, "指定")
        spec, real = self._same(self.lines, name)
        if spec and not real.startswith("_"):
            return self._line_from_spec(name, spec, f"lines:{real}")
        # Mini Tokyo 3D の路線名（「東急東横線」→「東横線」なども）
        for rid, r in self.rail.items():
            rn = r["name"]
            if rid.startswith("Base."): continue
            if name == rn or (len(rn) >= 3 and name.endswith(rn)) or name + "線" == rn:
                return self._line_from_spec(name, {"mt3d": [rid]}, f"mt3d:{rn}")
        return None

    # ------------------------------------------------------------ 入口
    def resolve(self, p):
        """p = {"name":..., "type": "point|area|line|auto", ...}。見つからなければ kind=none"""
        if isinstance(p, str): p = {"name": p}
        name = p["name"]
        disp = name
        name = self.aliases.get(name, name)          # 表記ゆれ（サッポロ→札幌 など）
        auto_none = (p.get("type") in (None, "auto")) and name in self.ignore
        if p.get("show") is False or p.get("type") == "none" or auto_none:
            f = Feature(name, "none", source="地図に出さない（範囲が広い・海外・場所不明）" if auto_none else "地図に出さない")
        else:
            t = p.get("type") or "auto"
            if t == "auto": t = self.guess_type(name)
            f = None
            if t == "line": f = self.resolve_line(name, p)
            elif t == "area": f = self.resolve_area(name, p)
            if f is None:
                f = self.resolve_point(name, p)
                if f is not None and t != "point":
                    f.source += f"（{t}が見つからず点で表示）"
            if f is None:
                f = Feature(name, "none", source="場所が分からない（lonlat を指定してください）")
        f.label = p.get("label", disp)
        f.name = disp
        if f.kind == "point" and p.get("side"): f.side = p["side"]
        return f


def _point_along(path, t):
    L = [0.0]
    for a, b in zip(path, path[1:]):
        L.append(L[-1] + math.hypot(b[0]-a[0], b[1]-a[1]))
    target = L[-1] * t
    for i in range(1, len(L)):
        if L[i] >= target:
            seg = L[i] - L[i-1] or 1
            u = (target - L[i-1]) / seg
            a, b = path[i-1], path[i]
            return (a[0] + (b[0]-a[0])*u, a[1] + (b[1]-a[1])*u)
    return path[-1]
