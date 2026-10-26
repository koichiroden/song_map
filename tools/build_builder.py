"""builder/template.html に曲データと辞書を埋め込んで builder/index.html をつくる。
data/songs.json や辞書を更新したら実行する:  python tools/build_builder.py"""
import json, os
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def L(n): return json.load(open(os.path.join(R, "data", n), encoding="utf-8"))
songs = L("songs.json")
PREFS = [f["properties"]["name"] for f in L("prefectures.json")["features"]]
# 地域 → 都道府県（並び順もこの順）。全国・広域は最後
SPOT_ORDER = [
 ("道北",1),("道央",1),("札幌",1),("道南",1),("道東",1),
 ("津軽",2),("青森",2),("岩手",3),("仙台",4),("宮城",4),("秋田",5),("山形",6),("福島",7),("東北（みちのく）",7.5),
 ("茨城",8),("栃木",9),("群馬",10),("埼玉",11),("房総",12),("千葉",12),
 ("千代田区",13),("中央区",13),("港区",13),("新宿区",13),("文京区",13),("台東区",13),("墨田区",13),("品川区",13),
 ("目黒区",13),("大田区",13),("世田谷区",13),("渋谷区",13),("中野区",13),("杉並区",13),("豊島区",13),("練馬区",13),("足立区",13),
 ("葛飾区",13),("東京（区特定なし）",13),("多摩",13),("路線・道路（首都圏）",13),
 ("横浜",14),("川崎",14),("鎌倉・三浦半島",14),("湘南",14),("厚木・海老名",14),("小田原",14),("伊豆箱根",14.5),
 ("新潟",15),("富山",16),("北陸",16.5),("石川",17),("福井",18),("長野",20),("岐阜",21),("山梨",19),("静岡",22),("名古屋中心部",23),("三重",24),
 ("滋賀",25),("京都中心部",26),("京都（宇治）",26),("京都（舞鶴）",26),("大阪中心部",27),("大阪（吹田）",27),("兵庫",28),("奈良",29),("和歌山",30),
 ("鳥取",31),("島根",32),("岡山",33),("瀬戸内",33.5),("広島",34),("香川",37),("高知",39),
 ("博多",40),("福岡",40),("佐賀",41),("長崎",42),("熊本",43),("大分",44),("鹿児島",46),("九州各地",46.5),
 ("沖縄（全体）",47),("沖縄本島",47),("宮古・八重山",47),
]
ORD = {sp: i for i, (sp, _) in enumerate(SPOT_ORDER)}
PC = dict(SPOT_ORDER)
def pref_label(c):
    if c == 7.5: return "東北各地"
    if c == 14.5: return "神奈川県・静岡県"
    if c == 33.5: return "瀬戸内"
    if c == 16.5: return "北陸"
    if c == 46.5: return "九州各地"
    return PREFS[int(c)-1]
for r in songs:
    c = PC.get(r["spot"])
    r["pref"] = pref_label(c) if c else "全国・広域"
    r["_k"] = (c or 99, ORD.get(r["spot"], 999), r["id"])
missing = sorted({r["spot"] for r in songs if r["spot"] not in PC and r["area"] != "全国・広域"})
if missing: print("※ 都道府県が未設定の地域:", missing)
songs.sort(key=lambda r: r["_k"])
for r in songs: del r["_k"]
dict_ = {
    "landmarks": sorted(L("landmarks.json")),
    "areas": sorted(L("areas.json")),
    "lines": sorted(k for k in L("lines.json") if not k.startswith("_")),
    "rails": sorted({r["name"] for k, r in L("kanto_rail.json").items() if not k.startswith("Base.")}),
    "stations": sorted(L("kanto_stations.json")),
    "munis": sorted(L("muni_index.json")),
    "prefs": [f["properties"]["name"] for f in L("prefectures.json")["features"]],
    "aliases": L("aliases.json"),
    "ignore": L("ignore.json"),
}
sample = json.load(open(os.path.join(R, "configs", "shonan.json"), encoding="utf-8"))
t = open(os.path.join(R, "builder", "template.html"), encoding="utf-8").read()
j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
t = t.replace("/*SONGS*/[]", j(songs)).replace("/*DICT*/{}", j(dict_)).replace("/*SAMPLE*/{}", j(sample))
open(os.path.join(R, "builder", "index.html"), "w", encoding="utf-8").write(t)
print("builder/index.html", len(t)//1024, "KB,", len(songs), "曲")
