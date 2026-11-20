# saccavid

Windowsでは **Code → Download ZIP** でダウンロードして展開します。Python 3.11 (64-bit、py ランチャー付き) と FFmpeg (ffmpeg / ffprobe / ffplay を PATH に設定) を用意し、**Start-Saccavid.cmd** をダブルクリックしてください。初回は編集用の依存パッケージをインストールします。

GUIで素材とキューを編集し、「書き出して確認」で480 fps動画を保存します。「実画面で再生」で出力動画と対象モニターを選べます。サンプルの素材は `py -3.11` の代わりにセットアップ済みの仮想環境を使い、`.venv\Scripts\python.exe make_test_input.py` で生成できます。対応する設定は `cues.example.json` です。

# saccavid v0.1 — 動画版 Saccaidance（エンコーダ + リアルタイムプレイヤー）

開発は自宅PC、実行は研究室PC。GitHub に上げて研究室PCで clone して使う想定:

```bash
git clone <this-repo> && cd saccavid
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt                  # + ffmpeg を PATH に入れておく
python sacca_player.py 素材.mp4 cues.json --refresh 480   # 本番提示
python saccavid.py encode 素材.mp4 cues.json -o out.mp4   # ファイル出力（検証用）
```

## sacca_player — 本番提示用プレイヤー

通常プレイヤーは 480fps ファイルのフレーム落ちを検出できず実験が静かに
壊れる。こちらは素材（通常fps）+ cues.json を読み、提示時にその場で
4サブフレーム分解し、vsync 同期で1リフレッシュに1サブフレームを出す。
全フリップを CSV に記録し、落ちたリフレッシュはログに残る
（`dropped_refreshes`）。タイミングモデルはエンコーダ v0.1 と同一で、
分解・デコードは別スレッドで先行実行する（480Hz では1スロット 2.08ms
しかなく、提示パス上で分解するとサイクル境界で落ちるため）。

- `--refresh 480` 提示レート（モニタ側も同じ Hz に設定しておく）
- `--windowed` 窓表示（動作確認用）
- `--test-mode` 画面なしのペーシング模擬（開発用。順序とログの検証のみで、実提示の保証にはならない）
- 音声は素材から抽出し最初のフリップと同時に再生（v0 はドリフト補正なし。再生後サマリの実測と drops を確認すること）

## saccavid.py — エンコーダ（ファイル出力・検証用）

AviUtl のような「タイムラインで印の出し入れを決める編集ソフト」の、
まず入出力だけを担う核。GUI はまだ無い。タイムラインは JSON で書く。

v0.1 は Codex による設計レビュー（`REVIEW_codex.md`）を受けて改訂した。
主な変更: 時間軸のバグ修正（下記「タイミングモデル」）、主張の格下げ
（論文一致は未確認であることの明記）、位相差 Δ のパラメータ化、
視角指定の追加、適用記録の出力、エラー処理の追加。

## 使い方

```bash
# 変換: 通常動画 (60fps等) + キューJSON -> 480fps Saccaidance動画
python3 saccavid.py encode input.mp4 cues.json -o out.mp4
python3 saccavid.py encode input.mp4 cues.json -o out.mp4 --delta 2 --gain 2.0

# 検査: fps と色サイクル (R/G/B/黒) の確認
python3 saccavid.py check out.mp4

# プレビュー (通常モニタでの診断用。通常の見え方そのものではない)
python3 saccavid.py preview out.mp4 --mode average -o avg.mp4   # 周期積分した診断画像
python3 saccavid.py preview out.mp4 --mode saccade -o sim.mp4   # 視線移動時の見え方の模擬
```

出力の横に `out.mp4.applied.json` が出る。実際に適用された
（周期境界へ量子化された）キューの開始・終了時刻と生成条件の記録。

## キュー JSON（タイムライン）

```json
{
  "display": {"screen_width_mm": 597, "distance_mm": 530},
  "cues": [
    {"start": 1.0, "end": 3.0, "x": 640, "y": 360,
     "pattern": "oval", "size": 220, "thickness": 6},
    {"start": 3.5, "end": 5.0, "nx": 0.25, "ny": 0.5,
     "pattern": "text", "text": "!", "size_deg": 4.0, "font": "/path/to/font.ttf"}
  ]
}
```

- `start` / `end`: 印を出す区間（秒）
- `x` / `y`: 中心位置（px）。`nx` / `ny`（0〜1 の割合）でも指定可
- `pattern`: `oval`（同心楕円）/ `radial`（放射）/ `circle` / `rect` / `cross` / `arrow` / `text`（記号・文字をそのままマスクに）
- `size` / `thickness`: px 指定。**表示条件に依存する量**で、原論文の
  6px は QHD 27インチ・視距離530mm での最適値（約0.30°）にすぎない。
  再現性が要る場合は `size_deg` / `thickness_deg`（視角）と上記の
  `display` ブロック（画面幅mm・視距離mm）で指定する
- `angle`: arrow の向き（度、-90 が上）
- `font`: text キューのフォントを固定する（実験用。未指定だと環境で字形が変わる）

## 色分解（v0.1 の暫定方式）と、その主張の範囲

各ピクセル (R,G,B) を 4 サブフレームのサイクルに分解する:

- 非パターン部: k=0:(R,0,0) → k=1:(0,G,0) → k=2:(0,0,B) → k=3:(0,0,0)
- パターン部: 同じサイクルを Δ（既定 1、`--delta 1|2|3`）だけ位相ずらし

これは **RGB 分離に基づく暫定方式であり、原論文／研究室実装と
スロット順序・黒スロット・振幅・位相差が一致するかは未確認**。
研究室のレシピを確認するまでは「論文の色分解の再現」とは言わない。

代数的に言えるのはここまで: 元画素とマスクが 1 周期の間一定なら、
4 サブフレームの RGB コード値の合計は両領域で元画素に一致する。
ただしこれは知覚的不可視性の証明ではない。ディスプレイ応答は
非線形なので、コード値の平均と発光量の平均は一致せず、彩度・色相の
保存も実機では保証されない。

明るさについて: 各チャンネルは 4 スロット中 1 回だけ提示されるため、
そのチャンネルの時間平均発光量は約 1/4 になる（黒スロットが主因
ではなく、チャンネルあたりのデューティが理由）。`--gain` は
コード値の乗算であり、校正された輝度補償ではない。クリップすると
チャンネル比＝色も変わる。

## タイミングモデル（v0.1 で修正）

- 出力時計が主時計。時間を 4 サブフレームの周期（480fps では 1/120 秒）
  に区切り、**元フレームとマスクは周期内では固定**し、周期の開始時刻で
  サンプリングする。
- これにより、入力が 25fps（v0 では +4.17% 伸長）・50fps（同 +25%）・
  59.94fps でも総時間が伸びず、かつ周期ごとの合計一致も保たれる。
- 代償として、キューの開始・終了と元フレームの切替は周期境界へ
  量子化される（誤差は最大 1 周期 = 約 8.3ms）。適用後の時刻は
  `.applied.json` に記録される。
- 入力は CFR 前提。VFR の PTS はまだ扱っていない。

## 制約（実験で使う前に必ず）

1. **出力は RGB ロスレス**（libx264rgb crf0 / FFV1）。普通の YUV 4:2:0
   エンコードは色サイクルを壊すので使えない。高解像度 480fps は
   H.264 のレベル・デコーダ対応の制約を受けるため、対象解像度での
   再生可否は別途確認が要る（FFV1+MKV や画像シーケンスも候補）。
2. **通常のプレイヤーで再生しないこと**。480fps でフレーム落ちすると
   積分が崩れて注視時にもチラつく。本番提示は vsync 固定＋フレーム
   落ち記録ができる再生プログラム側の仕事で、Codex レビューの推奨も
   「本番はリアルタイム分解を本線に、ファイル出力は検証用」。
3. **動画の動きそのもの**でも網膜像は動くので、印以外のエッジにも
   フリンジが出うる。最初はキューを静止区間に限定するのが安全。
4. 3 条件（印なし／明示／Saccaidance）比較では、印なし条件も同じ
   色分解を通すか通常映像にするかで意味が変わる。統制方針は未決定
   （`REVIEW_codex.md` の分岐点 3）。

## テスト

```bash
python3 make_test_input.py            # 320x240@60fps, 3秒
python3 saccavid.py encode tests/test_in.mp4 cues.example.json -o tests/test_out_480.mp4
python3 make_test_input.py 25 2 tests/test_in_25.mp4
python3 saccavid.py encode tests/test_in_25.mp4 cues.example.json -o tests/test_out_25.mp4
python3 verify_test.py
```

検証結果（2026-11-17, v0.1）: 60fps / 25fps とも周期合計と原画の
最大差 0、25fps の出力 duration は 2.000 秒（伸長なし）、
キュー内の位相ずれを確認。全て `verify_test.py` が assert で検査する。

## GUI エディタ

通常 fps の素材動画を見ながら、共通の cues JSON を編集する PySide6 アプリです。
この `saccavid/` ディレクトリを単独のプロジェクトとして使用できます。

### 準備と起動

Python 3.9 以降と、PATH 上の `ffmpeg` / `ffprobe` が必要です。
`requirements-editor.txt` は既存の `requirements.txt` を参照し、
PySide6 と OpenCV を追加します。Windows のコマンドプロンプトでは:

```bat
cd C:\path\to\saccavid
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements-editor.txt
python sacca_editor.py
```

macOS / Linux では、仮想環境を `source .venv/bin/activate` で有効化します。
動画と JSON を指定して開くこともできます:

```bash
python sacca_editor.py tests/test_in.mp4 cues.example.json
```

### 編集の流れ

1. **動画を開く**で素材を選びます。幅・高さ・fps・総時間を表示します。
   **再生／一時停止**、シークバー、**◀ 1f / 1f ▶** で位置を確認します。
2. **cues 読込**で既存 JSON を開くか、**cue 追加**で印を作ります。
   新しい cue は現在時刻から 1.5 秒、中央、oval、size=70、thickness=6 です。
   動画末尾を超える場合は、終了時刻を修正してから保存してください。
3. 一覧またはタイムラインで cue を選択します。一覧のダブルクリックで開始位置へ
   移動します。時間ブロックの本体をドラッグすると区間を移動し、左右端のドラッグで
   開始・終了を伸縮できます。**0.05 秒にスナップ**は切替可能です。
   ルーラーや空白のクリック／ドラッグでもシークできます。
4. プレビュー内のクリックまたはマーカーのドラッグで選択 cue の中心を動かします。
   右側で座標、開始／終了、パターン、サイズ、線幅を数値編集できます。
   arrow のときだけ角度、text のときだけ文字とフォントが有効になります。
   **複製／削除**は選択中の cue に作用します。
5. **display ブロックを保存**で画面幅・視距離を設定すると、px と deg の変換が
   できます。読み込んだ `size_deg` / `thickness_deg` も直接編集できます。
   座標形式の変換には動画、px/deg の変換には動画と有効な表示条件が必要です。
6. **cues 保存**または**名前を付けて保存**で保存します。不正な時刻、動画長超過、
   正でないサイズなどは日本語で表示し、該当 cue を一覧・タイムラインで強調します。
   不正値がある間は保存できません。未保存の変更があるときは、JSON の読込や終了時に
   保存・破棄・キャンセルを選べます。

新しい cue の座標は `nx` / `ny` です。読み込んだピクセル座標や軸ごとの混在、
任意の `display`、文書・cue 内の未知のフィールドは、明示的に編集した項目以外を保持します。
保存 JSON の日本語などは ASCII の `\uXXXX` エスケープになります。JSON としての値は
変わらず、既存ツールが Windows の既定文字コードで開いても読み込める形式です。

### プレビューと診断

マスクは既存の `make_mask()`、色分解は既存の `decompose()` を使用します。
**マスク重畳**を有効にすると、その時刻で有効なすべての cue を OR 結合し、
半透明の色で表示します。黄色の十字と輪は選択 cue の編集用マーカーです。

| モード | 表示する画像 |
| --- | --- |
| 通常 | 素材の RGB フレーム |
| 注視シミュレーション（診断用） | 4 サブフレームのコード値の合計を 4 で割った画像 |
| サッカードシミュレーション（診断用） | 各サブフレームを左へ `k × slip` px ずらした合計を 0〜255 にクリップした画像 |

`slip` は 1〜10 px、既定 3 px です。移動は既存の `_shift_zero()` を使い、
端を回り込ませずゼロで埋めます。位相差 Δ は 1〜3 で診断とエンコードに共通です。
重畳表示は診断画像にも追加されるため、診断画像だけを確認するときは **マスク重畳**を
OFF にしてください。gain はエンコードだけに適用します。

RGB 分離は暫定方式であり、原論文／研究室実装との一致は未確認です。
コード値の平均・移動和は診断用で、実際の知覚、発光量、サッカード、480Hz 提示の
正確さを保証するものではありません。

### 書き出しとプレイヤー連携

**エンコード**は保存した cues と素材を使い、既存エンコーダを別プロセスで実行します。
右側で Δ と gain を設定して出力先を選び、ログ欄で結果を確認できます。
実行中は **エンコードをキャンセル**を使用できます。出力は一時ファイルへ作り、
正常終了後に動画と `.applied.json` を出力先へ移します。失敗・キャンセル時は
一時ファイルを削除し、既存の出力動画を置き換えません。

**プレイヤーコマンド**は `sacca_player.py ... --refresh 480 --delta ...` を
クリップボードへコピーします。プレイヤーの起動は行いません。
Windows では PowerShell 用、macOS / Linux では POSIX シェル用の
引用形式です。本番提示は研究室の対応モニターで別途実施してください。

### 制約と構成ファイル

- 素材は既存エンコーダと同じく CFR 前提です。プレビューは素材フレーム単位であり、
  高負荷時は表示フレームを飛ばして再生時刻へ追いつきます。音声は再生しません。
  動画長は既存エンコーダと同じフレーム数 ÷ fps で扱います。
- 回転情報により OpenCV のデコード画像と ffprobe の解像度が異なる動画は、
  座標の誤適用を避けるためエラーにします。
- プレビューと本番で同じフォントファイルを使用してください。既存のフォント探索に
  よる代替字形は環境によって変わります。
- 動画のカット・結合、音声編集、複数動画・トラック、エフェクト、Undo/Redo はありません。
- Windows の実機 GUI 操作と480Hz モニターでの提示検証は、この通常 fps 編集ツールの
  自動検証と別に行ってください。

| ファイル | 役割 |
| --- | --- |
| `sacca_editor.py` | GUI、プロパティ、再生・シーク、保存、書き出し連携、スモーク CLI |
| `editor_model.py` | Qt 非依存の JSON 保持・検証・時間／座標操作 |
| `editor_preview.py` | OpenCV の RGB フレーム取得、共通マスク、診断画像 |
| `editor_timeline.py` | cue の時間ブロックとドラッグ編集 |
| `editor_export.py` | 既存エンコーダの起動、子プロセスの停止、一時出力の管理 |
| `requirements-editor.txt` | GUI の追加依存 |
| `tests/test_editor_*.py` | モデル、プレビュー、書き出し等の回帰検証 |

### GUI の検証コマンド

仮想環境を有効化した `saccavid/` ディレクトリで実行します。
pytest は検証用として別途インストールします:

```bash
python -m pip install pytest
python -m pytest tests/test_editor_model.py -q
python -m pytest tests/test_editor_preview.py tests/test_editor_export.py -q
python make_test_input.py
python sacca_editor.py --smoke-test tests/test_in.mp4 cues.example.json
python -m pytest tests -q
```

`--smoke-test` は画面を表示せずに、動画・JSON の読込、重畳フレームの描画、
一時 JSON の保存・再読込を確認します。成功時は `SMOKE OK`、終了コード 0 です。
必要なら `QT_QPA_PLATFORM=offscreen` を明示指定できます。
モデルのテストには `cues.example.json` のラウンドトリップ一致を含みます。

保存 JSON と既存エンコーダ・検証器の接続確認には、60fps と25fps の素材および
既存検証器が参照する出力を両方作り直します:

```bash
python -c "from editor_model import CueDocument; CueDocument.load('cues.example.json').save('tests/cues_gui.json', duration=3)"
python make_test_input.py
python saccavid.py encode tests/test_in.mp4 tests/cues_gui.json -o tests/test_out_gui.mp4
python saccavid.py encode tests/test_in.mp4 tests/cues_gui.json -o tests/test_out_480.mp4
python make_test_input.py 25 2 tests/test_in_25.mp4
python saccavid.py encode tests/test_in_25.mp4 tests/cues_gui.json -o tests/test_out_25.mp4
python verify_test.py
```

`saccavid.py`、`sacca_player.py`、`verify_test.py` の既存処理は変更していません。

「書き出して確認」（旧「エンコード」）でMP4を保存すると、その動画を読み戻した診断画面が開きます。
再生またはシークで、4サブフレームの移動和を確認できます。既存の480 fps出力は「書き出した動画を開く」から確認できます。


### GPU先読み再生と印の確認

「実画面で再生」で書き出したMP4とモニターを選び、「GPU先読み再生（事前展開・映像のみ）」を押します。
初回はMP4の全フレームを読み、動画と同じフォルダに `.sacca-cache` を作ります。動画によって数GB以上の空き容量が必要です。
準備が終わると再生に移ります。2回目からはキャッシュを再利用できます。キャッシュは削除すると次回作り直します。
再生中は最大3秒の表示順を保持し、使用済みテクスチャを再利用して次の画像を分割転送します。
終了後に初期枚数、開始後の補充枚数、領域再利用回数、枯渇回数と提示間隔を表示します。時刻はアプリ側で計測します。

印のある時刻を入力して「指定時刻の4枚で同期確認」を押すと、その周期だけを静止画として3秒間繰り返します。
Escで表示を終了できます。
