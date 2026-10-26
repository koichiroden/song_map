# ご当地ソング動画ジェネレーター

「〇〇のご当地ソング」という縦型ショート動画（1080×1920）をつくるツールです。
ビルダーで曲を選ぶと config の JSON ができ、それを Python に渡すと動画が書き出されます。

```
builder/index.html  ──(曲を選ぶ・地名を直す)──▶  configs/湘南.json  ──▶  python make_video.py  ──▶  out/湘南_gotouchi.mp4
```

## 使い方

### 1. セットアップ（Ubuntu）

```bash
sudo apt install ffmpeg fonts-noto-cjk libcairo2-dev pkg-config python3-dev
pip install -r requirements.txt
```

### 2. 曲を選んで config をつくる

`builder/index.html` をブラウザで開きます（ダブルクリックで開けます）。

1. 上の欄に動画タイトルの「〇〇」を入れる（湘南、東京、津軽など自由に）
2. 左の一覧で曲をクリック。**クリックした順番が動画の紹介順**になります（あとから ↑↓ やドラッグで並べ替え可）
3. 右の「地名・説明・出典を編集」で必要なら直す
   - 地名ごとに **点 / エリア / 線 / 出さない** を切り替え（自動で推測済み）
   - 位置がずれるときは座標欄に Google マップの「35.3126, 139.4209」形式で貼る
   - 出典は空欄なら出しません
4. 「JSON を保存」（またはコピーして `configs/〇〇.json` に保存）

作業中の内容はブラウザに自動保存されるので、閉じても続きから再開できます。
保存した JSON を「読み込んで続きを編集」に貼れば、あとから直すこともできます。

### 3. 動画を書き出す

```bash
python make_video.py configs/湘南.json --check            # 地名がどう解決されたか確認（速い）
python make_video.py configs/湘南.json --preview 2 10 20   # その秒のコマを PNG で確認
python make_video.py configs/湘南.json                    # 動画を書き出す → out/湘南_gotouchi.mp4
```

`--check` の表示の見方：`●`点 `▢`エリア `━`線 `×`地図に出さない（チップだけ表示）。
`×`で「場所が分からない」と出たら、ビルダーで座標を入れてください。

## 地図の出し方

| 種類 | 例 | 動画での見え方 |
|---|---|---|
| 点 | 横浜、横須賀、江ノ島、烏帽子岩 | ピン＋白枠のラベル |
| エリア | 湘南、津軽、房総、八重山、都道府県名 | 範囲をうっすら塗って輪郭線、エリア名を字間を空けて表示 |
| 線 | 江ノ電、小田急線、国道134号、神田川 | 線をビーっと引くアニメーション（鉄道＝白黒しま、道路＝オレンジ、川＝青） |

地名は次の順で探します。見つかった座標は `cache/geocode.json` に保存されます。

- **点**：config の座標 → `data/landmarks.json` → 首都圏の駅 → 市区町村 → 都道府県 → 国土地理院の地名検索 → OpenStreetMap（Nominatim）
- **エリア**：`data/areas.json`（市町村の集まりで定義）→ 都道府県 → 市区町村
- **線**：`data/lines.json` → Mini Tokyo 3D の首都圏路線（184路線）

よく使う地名・エリア・路線は JSON の辞書に足していけば、次からは検索なしで出せます。

```jsonc
// data/areas.json   エリア＝市町村の集まり（box で四角い範囲も可）
"湘南": {"pref": 14, "munis": ["藤沢市", "茅ヶ崎市", "平塚市", "鎌倉市", ...]},
"瀬戸内": {"box": [131.0, 33.75, 135.2, 34.75]},

// data/lines.json   mt3d＝Mini Tokyo 3D の路線ID、coords＝[経度, 緯度] の折れ線
"江ノ電": {"mt3d": ["Enoden.Enoden"]},
"国道134号": {"coords": [[139.585, 35.262], [139.578, 35.293], ...]},
"エボシライン": {"same": "国道134号"},

// data/landmarks.json   点の座標 [経度, 緯度]
"烏帽子岩": [139.4209, 35.3126]
```

## config の形

```jsonc
{
  "title": "湘南",                       // →「湘南のご当地ソング」
  "credit": "曲情報: Wikipedia「ご当地ソング」ほか",   // 最後の画面のクレジット（地図・路線の出典は自動で付く）
  "focus": "湘南",                       // イントロで輪郭を出すエリア（省略時はタイトル）
  "options": {"song_sec": 5.5},          // 1曲の秒数など
  "songs": [
    {
      "title": "希望の轍", "artist": "サザンオールスターズ", "kind": "歌詞に地名",
      "places": [
        {"name": "エボシライン", "type": "line"},
        {"name": "烏帽子岩", "type": "point", "lonlat": [139.4209, 35.3126]}
      ],
      "note": "カードの説明文", "source": "出典（空なら出さない）", "sec": 7
    }
  ]
}
```

地名の項目で使えるもの：`type`（point / area / line / none）、`label`（表示名を変える）、`lonlat`（点の座標）、
`munis`＋`pref` / `box`（その場でエリアを定義）、`coords` / `mt3d`（その場で線を定義）、`reverse`（線を引く向きを逆に）、
`side`（ラベルの位置 R / L / T / B）。曲ごとに `view: [西, 南, 東, 北]` でカメラ範囲を手動指定もできます。

## ファイル構成

```
make_video.py          動画を書き出すコマンド
gotouchi/world.py      地名 → 点・エリア・線
gotouchi/render.py     描画（pycairo → ffmpeg）
builder/index.html     曲選びのビルダー（tools/build_builder.py で生成）
builder/template.html  ビルダーの元
data/songs.json        曲リスト（823曲：アニソン・CMソング含む）
data/areas.json, lines.json, landmarks.json   地名の辞書
data/muni/             市区町村の形（47都道府県）
data/prefectures.json  都道府県の形
data/kanto_rail.json, kanto_stations.json     首都圏の路線と駅（Mini Tokyo 3D）
configs/shonan.json    サンプル
```

曲リストや辞書を更新したら `python tools/build_builder.py` でビルダーを作り直してください。

## データの出典

- 行政区域：国土数値情報（行政区域データ）を加工した [smartnews-smri/japan-topography](https://github.com/smartnews-smri/japan-topography)
- 首都圏の路線・駅：[Mini Tokyo 3D](https://github.com/nagix/mini-tokyo-3d)（MIT）
- 地名検索：国土地理院 地名検索API、OpenStreetMap Nominatim（© OpenStreetMap contributors）
- 首都圏以外の路線・道路・川の線は、駅などをつないだおおよその折れ線です
