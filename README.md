# KIHACHI Music AI v0.1

自然言語の音楽指示から、共通設計図 `SongSpec`、3本の標準MIDIファイル、音声生成向けプロンプトを作る最小動作版です。

```text
User Prompt
    ↓
Music Brain
    ↓
SongSpec
    ├── MIDI Composer → bass.mid / drums.mid / chords.mid
    └── Prompt Compiler → prompt.txt / prompt.json
```

AbletonGPTとAbleton Liveには接続しません。コアは標準ライブラリだけで動き、ACE-Step接続は任意のRESTアダプターへ分離しています。

## 導入

Python 3.11以降。編集可能モードで入れると、ソースを直した結果がそのまま反映されます。

```bash
python3 -m pip install -e .
```

以降 `PYTHONPATH=src` は不要です。呼び出しは `python3 -m kihachi_music_ai` を使います
（`kihachi` コマンドも入りますが、環境によってはスクリプト置き場がPATHに載っておらず、
モジュール形式のほうがどこでも確実に動きます）。

インストールせずに動かす場合は、すべてのコマンドの頭に `PYTHONPATH=src` を付けてください。

## 実行

リポジトリ直下から実行します。

```bash
python3 -m kihachi_music_ai compose \
  'Mutation Funk、DUB、Tech House。110 BPM、D#m。ファンキーなスラップベース。前半ミニマル、後半サイケデリック。Vocoderを使用。'
```

既定では `projects/mutation-signal/` に次を生成します。

```text
song_spec.json
bass.mid
drums.mid
chords.mid
prompt.txt
prompt.json
```

出力先を指定する場合:

```bash
python3 -m kihachi_music_ai compose '...' --output projects/my-song
```

既存の対象ファイルは上書きしません。意図して再生成するときだけ `--overwrite` を付けます。

## Lyrics（歌詞を「文章」ではなく「パート」として書く）

設計書が立てている区別をそのまま実装しています。**文学的に良い歌詞**と**音楽的に使いやすい歌詞**は別物です。Vocoderは文章を欲しがりません。キャリアを通して4小節ごとに繰り返されても成立する2〜3語の命令形を欲しがります。

そのためライターはまず**声の処理方法**で駆動され、テーマは二番目です。モードがフレーズ長・反復・フックの戻り方を決め、SongSpecのジャンルと気分が語彙を決め、`section.vocal_probability` がどのセクションに語を書くかを決めます（ただし**それは歌詞シートの中身を決めるだけで、実際にどこで歌うかは決まりません** — [構造タグは受理されますが、守られません](#構造タグは受理されますが守られません)）。

```bash
python3 -m kihachi_music_ai lyrics projects/my-song
```

```
- vocal mode: vocoder
- hook: Echo the floor
- lines: 26
    minimal_intro      [inst]     (no vocal)
    minimal_groove     [verse]    Bend Echo / Drive the spiral
    mutation_groove    [verse]    Switch again / Echo the wire / Pulse Floor / Funk Shadow
    psychedelic_drop   [chorus]   Echo the floor / Code Floor / Night Space / Echo the signal
    dub_breakdown      [bridge]   Dub Bend / Groove Wire
    final_drop         [chorus]   Echo the floor / Warp downtown / Drift the space / Lock Move
    outro              [bridge]   Bend the signal / Drift underground
```

| モード | 判定 | 書き方 |
|---|---|---|
| `vocoder` | `vocal.vocoder`、または character に robot/vocoder | 2〜4語、命令形と複合名詞 |
| `chant` | character に chant | 冒頭句を反復 |
| `spoken` | character に spoken | やや長い行 |
| `sung` | 上記以外 | 完全な行 |
| `instrumental` | `vocal.enabled` が false | 語を一切書かない |

* **フックは全ての chorus を開きます。** 反復こそがフックの目的で、再帰句のないVocoderパートはただのノイズに聞こえます。
* **語彙は曲自身から引きます。** ジャンルを重み順に並べ、`darkness >= 0.5` で暗い名詞、`psychedelic >= 0.5` で幻覚的な名詞を足します。
* **決定論的**です。同じseedからは常に同じ歌詞になります。

### 配線

`compose` が `lyrics.txt` を書き、`ace-step` はそれを**自動で歌います**。これまでは書き出したファイルを誰も送らず、全てのレンダーが空の `lyrics` で出ていました。`--lyrics-file` で差し替え、`--no-lyrics` でインストゥルメンタルにできます。

`stage-repaint` は歌詞シートがあれば複製し、無くても動きます（このモジュール以前に作られたプロジェクトにはシートがないため）。

### 構造タグは受理されますが、守られません

出力書式はACE-Stepの角括弧構造タグ（`[verse]` / `[chorus]` / `[bridge]` / `[inst]`）を使っています。2026-08-13に実サーバー（vast.ai上の`acestep-v15-turbo`）へ通しました。

**受理はされます。**`[inst]` / `[verse]`×2 / `[chorus]` を含む歌詞シートが拒否も無視もされず、`ace_step_result.json`の`metas.lyrics`へそのまま反映されます。尺は要求69.818sに対し出力69.80s、BPMは要求110に対し推定110.5でした。

**しかし守られません。**冒頭8小節を`[inst]`にした歌詞シートで生成したところ、**その区間に歌が入りました**（人間の聴取による判定、`decision_log.json`に記録）。タグはテキストとして受け取られるだけで、どこで歌うかの制御には使われていません。

**`section.vocal_probability`は届いていないのではなく、無視されています。**この区別は重要です。同じ生成で、プロンプトのArrangement行は既にこう書いていました。

```
Arrangement: minimal intro (8 bars, energy 0.25, no vocal); ...
```

歌詞シートの`[inst]`と、散文の`no vocal` — **2つの独立した経路で同じことを指示して、どちらも通りませんでした**。プロンプトの書き方を変えても解決は見込めません。

セクション境界そのものの再現度も高くはありません（この生成でrecall 0.667、エネルギー相関0.404）。

### セクションを器楽にする方法: repaintに歌詞を渡さない

**歌詞で「歌うな」と伝える経路は全滅ですが、歌詞そのものを渡さなければ効きます。**モデルは指示を読みませんが、無い語は歌えません。

repaintは自前の`lyrics`フィールドを持つので、`--no-lyrics`をセクション指定と組み合わせると**その区間だけ**歌詞なしで描き直せます。

```bash
# まず通常どおりレンダーし、
python3 -m kihachi_music_ai ace-step render projects/my-song \
  --base-url http://127.0.0.1:8001

# 器楽にしたいセクションだけを歌詞なしで描き直す
python3 -m kihachi_music_ai ace-step render projects/my-song \
  --task-type repaint --repaint-section psychedelic_drop --no-lyrics \
  --source-audio projects/my-song/audio/ace-step-01.wav \
  --base-url http://127.0.0.1:8001 --overwrite
```

2026-08-13に実サーバーで確認しました。`[chorus]`として歌が入っていた25〜32小節（52.4秒以降）を歌詞なしでrepaintしたところ、**その区間は器楽になりました**（人間の聴取による判定）。送信内容は`ace_step_repaint_request.json`に`lyrics: ""`、`repainting_start/end`が52.364〜69.818として残ります。

`--task-type repaint`は必須です（セクション/小節セレクタだけでは拒否されます）。repaintのリクエストは`ace_step_repaint_request.json`へ別に書かれるので、元の`ace_step_request.json`はtext2musicのまま残ります。

これで`vocal_probability`の意図を**間接的に**実現できます。ライターが`[inst]`を置いたセクションを、そのままrepaintの対象にすればよいためです。

#### 対象セクションは`instrumental-plan`が教えます

どのセクションが`[inst]`だったかを歌詞シートから読み取って手で打ち直す必要はありません。

```bash
python3 -m kihachi_music_ai instrumental-plan projects/my-song
```

```
Instrumental sections for projects/my-song:
- the lyric sheet left these sections wordless; the model ignores an instruction
  not to sing, so the words have to be withheld instead
    bars 0:7  minimal_intro  (energy 0.25, vocal_probability 0.00)
- run these in order, each against the previous take:
    python3 -m kihachi_music_ai ace-step render projects/my-song --task-type repaint \
      --repaint-section minimal_intro --no-lyrics \
      --source-audio projects/my-song/audio/ace-step-01.wav --base-url ... --overwrite
```

判定規則を再実装してはいません。`lyrics.build_lyrics`にそのまま尋ねるので、**ライターが沈黙を決める条件を変えても、repaintの対象がずれません**。`--save`で`instrumental_plan.json`を残せます。

**コマンドは表示するだけで実行しません。**repaintは数分のGPUを使い、テイクを上書きするので、走らせる判断は呼び出し側に残します。SongSpecが`vocal.enabled = false`なら、曲全体が既に器楽なのでrepaintは不要である旨を表示します。

## Prompt Compiler（全ての記述をSongSpecの数値から導出）

以前のプロンプトは `"octave pops, ghost notes"` `"spacious dub gaps"` `"tape-delay tails"` といった語を**ハードコード**していました。SongSpecの数値が何であれ同じ文言が出るため、`chords.dub_delay` や `section.fx_amount` を変えてもAudioモデルには一切届きませんでした。Spec Diffが「変更した」と報告できるのに、それが着地しようがない状態です。

`band()` は 0..1 の値を、数値のダンプではなく音楽モデルが反応する語へ変換します。Reviewerが引用し直す値（セクションのenergy）は数値のまま残し、revision promptとの一貫性を保ちます。

```
Mood: dark, deeply psychedelic.
Groove: heavily syncopated, off-beat led, swung 0.54, machine-tight.
Bass: dominant and up-front slap electric bass, restlessly syncopated phrasing,
      occasional ghost notes, occasional octave pops, mutation intensity 0.78.
Drums: syncopated tech house, driving four-on-the-floor kick, relentless 16th hats, spacious dub gaps.
Harmony: D#m - B - F# - C#, one chord per bar; dub chord stab, short offbeat stabs, tape-delay tails.
Vocal: vocoded dark robotic phrases.
Arrangement: ... dub breakdown (16 bars, energy 0.28, no drums, drenched in dub fx, heavily psychedelic);
      ... Overall: build hard from a quiet opening to a peak of 0.95 at final drop,
      then land on outro at 0.22, never losing the dance groove.
```

### 未使用パラメータは全て解消しました

全数監査の結果、どこからも読まれていなかったのは7つでした。

`style.darkness` / `bass.role` / `chords.instrument` / `chords.articulation` / `vocal.vocoder` / `arrangement[].fx_amount` / `arrangement[].vocal_probability`

現在は**SongSpecの全フィールドが消費されています**。加えて、Composerだけが読んでいてプロンプトには反映されていなかった値（`groove.humanize`、`bass.syncopation`、`drums.dub_space`、`chords.dub_delay` など）も文言に反映されるようになりました。

```
$ python3 -m kihachi_music_ai apply-edit ... "dub_breakdownのディレイをかなり増やして"
- sections regenerated: none          ← 音符は動かない（正しい）
- audio prompt changed: True          ← 以前は False だった

- Harmony: ... short offbeat stabs, tape-delay tails.
+ Harmony: ... short offbeat stabs, long dub tape-delay tails.
- Production: deep sub control, wide dub echoes, ...
+ Production: deep sub control, cavernous dub echoes, ...
```

### エネルギー曲線はピークで記述します

最終セクションと最初のセクションを比べる実装だと、0.25から0.95まで駆け上がって静かなoutroで着地する曲が「hold one level throughout」になってしまいます。ピーク位置と、そこから落ちる場合の着地点を明示します。

### プロンプト長

セクション単位の記述が最も長くなるため、1セクションあたりの特徴は最大3つに制限しています。実測: 32小節 1025文字、5分尺 1447文字。600文字のrevisionを前置しても 2108 文字で、revisionは常に先頭に置かれるので切り詰めが起きても優先度の高い制約は残ります。

## 差分命令（曲全体を作り直さずに一箇所だけ変える）

`edit.py` は短い指示を **Spec Diff** — before/after を明示した検証可能なパラメータ変更リスト — に変換します。計画と適用は分かれていて、適用は常に**新しいプロジェクト**へ書きます。

```bash
python3 -m kihachi_music_ai edit projects/my-song "Dropのベースだけもっと変態的に"
```

```
- reading: mutation increase by 0.2
- target: bass in psychedelic_drop, final_drop
    psychedelic_drop: mutation 0.88 -> 1.0
    final_drop: mutation 0.95 -> 1.0
- plan: projects/my-song/spec_edit.json (nothing regenerated yet)
```

```bash
python3 -m kihachi_music_ai apply-edit projects/my-song projects/my-song-v2
```

```
- sections regenerated: ['psychedelic_drop', 'final_drop']
- sections byte-identical: 7
    bass: 856 -> 860 notes, changed in psychedelic_drop, final_drop
    chords: 1008 -> 1008 notes, unchanged
    drums: 1430 -> 1430 notes, unchanged
```

### 局所性はComposerの性質

各セクションが **自分専用の乱数ストリーム** (`seed:track:section_index`) を持ちます。1本のストリームを共有していると、あるセクションの音数が変わっただけでストリーム位置がずれ、それ以降の全セクションが動いてしまいます。分離したことで、編集していないセクションと編集していないトラックの`.mid`は**バイト単位で同一**になります（回帰テストで固定）。

`edit_report.json` はそれを事後に証明します。推測ではなく、実際に書かれた音符をセクションごとに突き合わせた結果です。

### 「だけ」は本当に「だけ」

セクションを指定した場合、曲全体スコープのパラメータは使いません。`bass.mutation` は曲全体の値なので、これを動かすと他の全セクションのベースに波及してしまうからです。そのqualityに**セクション単位のフィールドが存在しない**場合のみ曲全体の値を使い、そのときは `scope_warnings` に明示します。

```
! syncopation has no per-section parameter, so bass.syncopation, groove.syncopation
  changes for the whole song, not only psychedelic_drop
```

### 認識する語

| 対象 | 語 |
|---|---|
| パート | ベース/bass/低音/スラップ、ドラム/kick/パーカッション、コード/シンセ/stab |
| 範囲 | セクション名、drop/ドロップ、breakdown、intro、build、outro、前半/後半 |
| 質 | 変態/mutation、シンコペ、密度/厚く、激しく/energy、ゴースト、オクターブ、スペース/抜いて、ディレイ/fx |
| 方向 | もっと/上げ/増やし ↔ 抑え/減らし/下げ/薄く |
| 強さ | 少し (0.1) / 既定 (0.2) / かなり (0.35) |

セクション名は識別子として扱い、走査前に除去します（`dub_breakdown` の中の "down" が減少語として誤認されるため）。ASCII語は語頭境界を要求します（"group" の中の "up" を弾き、かつ "densely" は "dense" にマッチさせるため）。

### 効果がない編集は、そう報告します

一部のSongSpec値は現状どこにも届きません（`chords.dub_delay` はプロンプトがテキストを固定しているため、`fx_amount` と `vocal_probability` はまだ読まれていません）。また量子化幅より小さい密度変更は音符を動かしません。その場合 `apply-edit` は `no_effect` を立てて警告します。黙って成功したふりはしません。

## Arrangement Engine（曲がどう時間を使うか）

以前は長さに関係なく常に「4等分」でした。32小節のスケッチなら妥当ですが、それ以上では破綻します。5分の曲が「34小節のイントロ + 34小節 × 3」になり、Breakdownも2度目のDropもありませんでした。

`arrangement.py` は使える8小節ブロック数に応じて**セクション・アーキタイプの並び**を選び、各セクションに固有のパート別密度とアクティブトラックを与えます。

```bash
python3 -m kihachi_music_ai compose '... 5分程度。'
```

```
- arrangement: 9 sections over 136 bars (296.7s)
    bar    1 +16  minimal_intro      energy 0.25
    bar   17 +16  minimal_groove     energy 0.44
    bar   33 +16  mutation_groove    energy 0.62
    bar   49 +16  mutation_build_1   energy 0.66
    bar   65 +16  psychedelic_drop   energy 0.88
    bar   81 +16  dub_breakdown      energy 0.28  (resting: drums)
    bar   97 +16  mutation_build_2   energy 0.66
    bar  113 +16  final_drop         energy 0.95
    bar  129 +8   outro              energy 0.22  (resting: drums)
```

設計上の要点:

* **8小節フレーズに揃う。** ダンス構成は8小節単位で数えるので、15小節のBreakdownは単純に誤りです。1セクション1ブロックを配ったうえで、余りをアーキタイプの重み（Dropはイントロより長い）で最大剰余法により配分します。
* **`active_tracks` は実際に音を止める。** `dub_breakdown` はドラムを完全に抜きます（`drum_density 0.0` + `active_tracks ("bass","chords")`）。「低エネルギーと書いてあるだけ」ではありません。休符はMIDI Criticでも「設計通りの休符」として扱われ、coverage減点になりません。
* **セクション名は一意。** アークがアーキタイプを再利用する場合（最終Drop前の2度目のBuild）、`mutation_build_1` / `mutation_build_2` と採番します。同名だと `--repaint-section` が黙って最初の一つを選んでしまうためです。
* **パート別密度。** `bass_density` / `drum_density` / `chord_density` / `fx_amount` / `vocal_probability` / `mutation` を各セクションが持ちます。「ベースだけ厚く」がベースだけに効きます。
* **`mutation` はセクションの変異強度**、SongSpecのパート値は「そのパートがどれだけ反応するか」です。積を取るので `0.0` は本当に完全反復になります。

### 後方互換性

**32小節では従来の4セクション構成をフィールド単位で完全に再現します。** 既存プロジェクトはrepaint計画をSongSpecのSHA-256にピン留めしているためです。

新しい任意フィールドは未設定なら`to_dict`が省略するので、**エンジン導入前に書かれた`song_spec.json`はバイト単位で同一にシリアライズされ、SHA-256も変わりません**（`bc83df…f420f93` を回帰テストで固定）。未設定のセクションは全パート密度が`energy`にフォールバックするため、既存プロジェクトの作曲結果も一切変わりません。

ただし**同じプロンプトから新規にcomposeしたSongSpecは、エンジンの詳細を持つためハッシュが変わります**（`ca88be5b…`）。`name` / `start_bar` / `length_bars` / `energy` / `minimal` / `psychedelic` は同一で、追加は加算のみです。既存プロジェクトと新規composeを `review --against` で比較することはできません（SongSpec一致が必要なため）。

## Mutation Engine（反復の中の突然変異）

`mutation.py` はMutation Funkを「ジャンル名」ではなく**作曲アルゴリズム**として実装した独立モジュールです。パートは1小節のベースパターンとして一度だけ書かれ、以降の各小節は**直前の小節**を変異させたものになります。毎小節を独立に乱数生成するのではないので、セクションは「ひとつのアイデアが押し進められていく」ように聞こえます。

```
A → A' → A'' → A'''
```

`amount` が1小節あたり何音変えるかを決め、SongSpecの各確率がどの操作を起こすかを決めます。操作は displace（16分単位のシンコペーション）、ghost（弱音の装飾音）、octave（オクターブ跳躍）、drop（間を作る）、accent（velocity変化）です。

どの `amount` でも次の2つは必ず保たれます。これが `preserve_groove` / `preserve_key` にあたります。

* **アンカー**（ダウンビート、バックビート）は絶対に削除も移動もされない。どれだけ変異してもグルーヴが読める。
* **音高はオクターブ単位でしか動かない**。ハーモニーは変異の影響を受けない。

Composerは以下のSongSpec値を実際に読みます。

| SongSpec | 効果 |
|---|---|
| `arrangement[].energy` | 音数密度とvelocityを決める（全パート） |
| `arrangement[].psychedelic` | セクションごとの変異強度を押し上げる |
| `bass.mutation` | Bassの変異量。0.0で完全反復 |
| `bass.syncopation` | 密度とdisplaceの発生率 |
| `bass.ghost_note_probability` | ghost操作の重み |
| `bass.octave_jump_probability` | octave操作の重み |
| `drums.kick_density` / `hat_density` | キック音数、ハットの刻み |
| `drums.dub_space` | キックに穴を空ける（drop操作） |
| `chords.dub_delay` | ディレイの尾を残すためstabを間引く |
| `groove.swing` / `humanize` | マイクロタイミング |

実測（seed 8、EXAMPLE prompt）では、energyが 0.25 → 0.44 → 0.66 → 0.88 と上がるのに従って、Bassは 39 → 45 → 48 → 52音、velocity上限は 84 → 127 へ単調に増えます。一方でBassのピッチクラスは `{C#, D#, F#, B}` のまま — 進行 D#m - B - F# - C# のルートから外れません。

`mutation.py` は pure/stdlib で、`random.Random` を必ず引数として受け取るため、同じseedからは常に同じ演奏が出ます。

## テスト

開発用依存関係を入れて、ローカルとCIで同じ全テストを実行します。

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m build
```

Pull Requestと`main`へのpushでは、GitHub Actionsがサポート下限のPython 3.11と開発環境系統のPython 3.14で全テストを自動実行します。Python 3.11ではさらにsdistとwheelを作成し、checkout外の隔離環境へwheelを導入して`kihachi compose`の成果物まで検証します。Vast/ACE-Stepへの実接続はGPU・ネットワーク・秘密情報に依存するため、このCIには含めません。

## Critic の二経路（MIDI照合 と Audio解析）

Analyzerは**完成ミックスから**ハーモニーを推定します。この方式には原理的な上限があり、実測でもキー信頼度 0.11、コード一致率は 29テイク中18テイクで 0.0、残りも最大 0.167 でした。キック・ベース・コード・ボーカル・dub delayが同じ帯域で重なるため、検出器には見えません。

**この数字は検出器の実装バグではありません。**2026-08-15に切り分けました。SongSpecの進行（D#m - B - F# - C#）を56小節ぶん合成してそのまま`analyze`へ通すと、**一致率 1.0 / coverage 1.0 / キー推定も正解**になります。推定器単体（理想クロマ24種）とchroma抽出込み（合成三和音24種、低音3倍・倍音4本まで含む）も全て正解でした。小節の切り出し、進行のインデックス、grid整合に誤りはありません。

したがって`chord progression match`が低いことを見て**実装を疑う必要はありません**。クリーンな信号なら測れるものが、実ミックスでは測れない — それが上限の正体です。

もう一つ実測があります。全テイクの信頼可能な小節493件で、検出されたルートと期待されたルートの音程差を集計すると、**最頻値はルート一致（14.6%）ではなく完全5度上（21.3%）**でした。低域が65〜69%を占める素材で、検出器がルートより5度を拾っていることを示します。マスキングの内訳としては妥当ですが、**「モデルが進行を弾いていない」証拠にはなりません** — それを言うにはステム分離した音源が要ります。

しかしMIDIをSongSpecから書いている以上、**ハーモニーは推定する必要がありません。既に分かっています。** `midi_review.py` はディスク上の`.mid`を読み戻し、SongSpecと厳密照合します。

```bash
python3 -m kihachi_music_ai midi-review projects/my-song
```

```
- midi alignment score: 99.3 (aligned)
- harmony: bass-root match 1.0, chord-tone match 1.0 (progression D#m - B - F# - C#)
- key: 0/415 pitched notes outside D# minor
- written energy correlation: 0.9766
- coverage: 1.0
```

Audio解析が不要なので、`compose`直後の（まだ音を生成していない）プロジェクトにも使えます。

`review` は両方を出します。同じ楽曲で **Audio 58.34 (partial) / MIDI 97.01 (aligned)** となり、これが2つの状況を区別します。

| 状況 | MIDI | Audio | 意味 |
|---|---|---|---|
| `harmony_written_but_not_detected` (info) | 一致 | 不一致 | 設計は正しい。検出限界か、音として埋もれている。**repaintで「直す」べきではない** |
| `midi_harmony_misaligned` (high) | 不一致 | — | 作曲そのものが誤り。repaintでは直らないので先に作曲を修正する |

MIDI由来のfindingはrepaintのrevision promptには載りません（あれはAudio生成への指示なので）。

`midi.py` には `read_midi()`（format 0パーサ）を追加しました。SongSpecからの再生成ではなく**実ファイルを読む**ので、ディスク上の成果物そのものを検証します。書き出し→読み戻しはPPQ 480グリッド上で完全一致します。

## 生成Audioの解析

ACE-Stepで生成したWAVを読み取り専用で解析し、SongSpecの尺、BPM、キー、コード進行、セクション/エネルギー設計に照合します。

```bash
python3 -m kihachi_music_ai analyze \
  projects/mutation-signal
```

既定では`audio/ace-step-01.wav`を読み、`audio_analysis.json`へ次を保存します。

- WAV形式、sample rate、channel数、bit depth、実尺、SHA-256
- peak / RMS / crest factor / DC offset
- clipping sample率と-50 dBFS未満の無音window率
- 20ms RMS energy変化のautocorrelationによる推定BPMと信頼度
- 48音のGoertzel chromaによる推定キー、信頼度、pitch class profile
- SongSpecの小節grid上の推定コード、信頼度、信頼可能な小節のcoverage、進行一致率
- 小節ごとのRMS energy、局所dB差によるセクション境界、SongSpecとの境界再現率とenergy相関（後半の編集が前半の境界閾値を動かさない方式）
- SongSpecの目標尺・BPM・キー・コード・構成との差と品質flag

WAVやMIDIは変更しません。既存の`audio_analysis.json`も`--overwrite`なしでは置換しません。キー/コード/境界は完成mixに対する軽量な推定なので、必ず`confidence`、`reliable`、`confident_bar_coverage`と一緒に扱います。

## 解析フィードバック

`audio_analysis.json`とSongSpecの差から、整合度と再生成方針を作れます。

```bash
python3 -m kihachi_music_ai review \
  example_output/mutation-signal-lora \
  --against example_output/mutation-signal
```

対象projectへ次を追加します。

```text
generation_review.json
revision_prompt.txt
repaint_plan.json
```

`generation_review.json`には、尺、tempo、key、chord、section境界、section energyの重み付き整合スコア、根拠付きfinding、Baseとの差を保存します。このスコアはSongSpecへの機械的な整合度であり、音質や音楽的な良さの評価ではありません。

`repaint_plan.json`は、各セクションのenergy差、コード一致率、判読可能なコード小節率、開始境界、終端energy失速を比較し、最も修正優先度が高いセクションを1つ選びます。素材検査がクリック疑いのdiscontinuityを時刻付きで検出した場合は、構成スコアより素材欠陥を優先し、その時刻を含む前後4小節をbar-level範囲として選びます。選択小節・秒範囲、局所的な改訂文、安全なrepaint設定、元解析AudioのSHA-256を保存します。計画作成だけでは生成を開始しません。
discontinuity判定は最大sample jumpをその前後10 msの平均slewと比較します。曲全体の静かな区間を分母にしないため、孤立したsplice stepは検出しつつ、キックなど連続した高速過渡音をクリックとして追い続けません。

Reviewerの計画をACE-Step requestへ変換できます。この段階もネットワーク送信しません。

```bash
python3 -m kihachi_music_ai ace-step prepare \
  example_output/mutation-signal-lora \
  --repaint-plan repaint_plan.json
```

選択セクションとSongSpecのSHA-256が一致しない計画は拒否されます。`review`はSongSpec、解析JSON、WAV、MIDIを変更せず、既存review成果物も`--overwrite`なしでは置換しません。

実生成は元プロジェクトと分離します。次のコマンドはSongSpec、MIDI、prompt、repaint計画だけを新規ディレクトリへ複製し、元AudioはSHA-256を確認するだけでコピーしません。適用した計画は`applied_repaint_plan.json`にも固定保存され、生成後のReviewerが作る次の計画と区別されます。出力先が既に存在する場合は停止します。

```bash
python3 -m kihachi_music_ai ace-step stage-repaint \
  example_output/mutation-signal-lora \
  example_output/mutation-signal-lora-repaint-auto-01
```

作成されたプロジェクトで`ace-step render --repaint-plan repaint_plan.json`を実行し、`--source-audio`には検証済みの元Audioを明示します。

再生成前に、元のACE-Step requestを残したまま改訂版requestを確認できます。この段階ではネットワーク送信しません。

```bash
python3 -m kihachi_music_ai ace-step prepare \
  example_output/mutation-signal-lora \
  --revision-file example_output/mutation-signal-lora/revision_prompt.txt
```

改訂版は`ace_step_revision_request.json`へ保存されます。`bpm`、`key_scale`、`time_signature`、尺、seedなどの構造化フィールドはSongSpecの値を維持し、修正指示はprompt先頭の`Revision constraints`へ優先配置します。

確認後に実際の生成へ使う場合は、`ace-step render`にも同じ`--revision-file`を明示します。生成結果JSONには適用したrevisionのSHA-256とrequestファイル名が記録されます。

## 帯域バランス（Criticが「ベースが弱い」を言えるようにする）

`analyze`が全帯域のエネルギー配分を測り、`review`が群から外れたテイクを指摘します。

```
- spectral balance: sub 5% bass 84% low_mid 4% mid 6% high_mid 1% high 0%
- low/high ratio: 64.373 (corpus median 21.9), centroid 258 Hz
- dull_high_end: 6 kHz以上が0.1%しかない
- bass_masking: 60-250 Hzに83.7%が集中
```

**閾値は実レンダー21本から較正しました。一般的なミックスの理想値ではありません。**
この生成器の出力は中央値で**エネルギーの63%が60–250 Hz**に集中しており、
一般的な基準で判定すると全部が不合格になって何も言えなくなります。
21本のうち外れるのは2本だけで、どちらも高域がほぼ無いテイクです。

副産物としてLoRAの効果を測れるようになりましたが、**方向は安定しません**。
8ステップ・vast.aiでの対（seed 8、他条件同一）は6 kHz以上が0.1%→1.6%の16倍でしたが、
60ステップ・ローカルで測り直すと同じseed 8で0.58倍と逆に出ます（seed 42では2.65倍）。
高域についてLoRAが何をするかは、まだ言えません。

2つの対で方向が一致したのは別の量です — **低域(60-250 Hz)が減り、high_mid(2.5-6 kHz)が増える**。
0.674→0.497 / 0.626→0.521、および3.6倍 / 2.7倍。n=2なので示唆であって確定ではありません。

FFTは標準ライブラリに無いため自前で書いています（radix-2、テストで定義式と照合済み）。
窓は全長に分散して最大200枚なので、5分尺でも70秒尺でも約2.3秒です。

## ラウドネス（ITU-R BS.1770-4）

PeakとRMSは知覚音量と対応しません。Peakは1サンプルで決まり、RMSは40 Hzと3 kHzを
同じ重みで扱います（耳の感度は約20 dB違います）。

```bash
python3 -m kihachi_music_ai analyze projects/my-song --loudness
```

```
- integrated loudness: -19.11 LUFS (range 8.28 LU, 632/695 blocks kept)
```

**既定では走りません。** 全サンプルをフィルタするため70秒尺で約11秒、5分尺で約49秒かかり、
`revise`は`analyze`をループで呼びます。実測21本が−19.11〜−13.91 LUFSの5 LU幅に収まっており、
この生成器の弱点がラウドネスではないことも、常時払う理由が無い根拠です。

規格が係数を公開しているのは48 kHzだけなので、他のレートは双一次変換で導出しています。
48 kHzで公開値を浮動小数点誤差の範囲で再現することをテストで固定しました。
一般的なEQのレシピから導くと**規格値を外し**、−23 LUFSの基準音が−23.26と出ます
（許容±0.1を超えますが、壊れているようには見えない類の誤りです）。

True peakは未実装です。インターサンプルピークの検出にはオーバーサンプリングが必要で、
このプロジェクトがまだ到達していないマスタリング段の話になります。

## Groove（指定した揺れが本当に鳴っているか）

SongSpecは`swing`と`humanize`を持ちますが、それが実現しているか誰も確かめていませんでした。

```
- groove: offbeats 7.738 ms late (swing 0.54 asks 7.636, off by +0.102); humanize jitter 0.737 ms
```

**測る場所はMIDIです。音声からは測れないことを実測で確認しました。**

110 BPMでswing 0.54が生む変位は7.6 ms、humanizeは±1.7 msです。既存の包絡線はホップ20 msで
そもそも見えないため、1 msの包絡線でオンセットを取る実装も書きました。合成クリックなら
仕込んだ7.6 msを誤差0.3 msで復元できます。しかし**実レンダー21本すべてで平均絶対偏差が約35 ms**
（110 BPMの16分音符の1/4）に達し、裏拍の値は−9〜+4 msに散らばって7.6 msにかすりもしません。
ダブのディレイ・テールと重なり合うパートのせいで、検出したオンセットが音符の頭を指していません。

ハーモニーがミックスから検出できずMIDIでは厳密だったのと同じ構図です。音声側の測定は
`reliable: false`と理由を添えて残してあり、判定には使いません。

## ローカルサーバ（CPU推論）での注意

ACE-StepをCPUで動かす場合、`--request-timeout` の既定は180秒です。30秒ではありません。

CPU推論はサーバのワーカーを生成中ずっと占有するため、状態確認のポーリングですら
その後ろで待たされます。30秒はGPU前提の値で、ローカルのIntel Macでは全ポーリングが
タイムアウトしました。タイムアウト時は設定名を含むメッセージが出ます。

```
ACE-Step did not answer within request_timeout=30s; raise it if the server is running on CPU
```

以前はこれが「response could not be decoded」と表示され、サーバのJSONを疑う方向へ
誘導していました。

## 指示の解釈（否定と強度を読む）

`MusicBrain` はプロンプトに対して**5つの真偽判定**を行い、その答えごとに2つの定数のどちらかを選んでいました。ここには性質の違う2つの欠陥がありました。

**否定が要求として読まれていました。**

```
"スラップじゃなくて指弾きで"  →  スラップ という部分文字列を含む  →  slap = True
```

`bass.technique` は `slap`、`bass.syncopation` は 0.86、`octave_jump` は 0.45、`ghost` は 0.34。**要求と正反対の4つの値**が書き込まれます。`"サイケじゃない"` は `style.psychedelic` を最大の 0.82 にしました。これは機能不足ではなく、明確に述べられた拒否をその逆として実行していたという**正しさの欠陥**です。

**強度に着地点がありませんでした。** `少しサイケ` も `かなりサイケ` も 0.82 です。真の側に 0.82 しか無いからです。しかもその語彙は**既にリポジトリの中にありました** — `edit.py` の `少し` / `かなり` は最初から効いていて、ただし**曲を直すときだけ**でした。同じ日本語が、打ったコマンドの名前によって通じたり通じなかったりしていたことになります。

`intent.py` はプロンプトを `Trait(name, polarity, strength)` へ分解します。

```
"スラップじゃなくて指弾きで。少しサイケ。"
    slap         polarity -1
    psychedelic  polarity +1  strength 0.5
```

| 入力 | 変更前 | 変更後 |
|---|---|---|
| `スラップじゃなくて指弾きで` | technique **slap** | technique `fingered` |
| `少しサイケ` | psychedelic 0.82 | **0.55** |
| `かなりサイケ` | psychedelic 0.82 | **1.0** |
| `アルペジオは無しで` | arpトラックを**追加** | 追加しない |

**否定は言語ごとに向きが違います。** 日本語は直前の言及に、英語は直後の言及に結びつけます。列挙は接続語だけで繋がっているときに限って広げます。

```
"スラップとサイケはなし"          → 両方とも拒否（と で繋がっている）
"ミニマルにしてサイケは無し"      → サイケだけ拒否（にして は接続語ではない）
"スラップじゃなくて指弾き。サイケに。"  → 節を越えない
```

**拒否は「低い極」に着地し、その先へは行きません。** ここで扱う特性はどれも、拒否された側の値＝言及が無いときの値です。それを下回る値を作るのは解釈ではなく捏造になります。否定が買うのは新しい値ではなく、**反対の値に着地しなくなること**です。

### 既存の曲が1ビットも動かない理由

`preferences` と同じ規律です。**中立を恒等写像にする**。

強度 1.0（＝素の言及）を `blend(low, high, 1.0) == high` と定義し、この `high` は従来ハードコードされていた定数そのものにしてあります。したがって強度語も否定語も含まないプロンプト — `example_output` の全てがそうです — は、以前と完全に同じSongSpecを出します。`song_spec_sha256` に固定された repaint 計画も、MIDIのバイト列も動きません。

### 凍っていた定数を解凍する

読み取れても着地点が定数しか無ければ意味がありません。`drums.kick_density` 0.72、`groove.humanize` 0.18、`harmonic_rhythm_bars` 1、`bass.role`、`chords.articulation` は**1020ジャンル全てで同一**でした。

| ジャンル | kick | humanize | harmonic rhythm | pattern |
|---|---|---|---|---|
| ダブ | 0.38 | 0.30 | 2小節 | `one_drop` |
| ドラムンベース | 0.50 | 0.10 | 4小節 | `breakbeat` |
| ボサノヴァ | 0.45 | 0.38 | 1小節 | `samba` |
| アンビエント | 0.18 | 0.35 | 4小節 | `sparse_pulse` |
| テクノ | 0.85 | 0.06 | 2小節 | `four_on_floor` |

**既定値は中立ではありませんでした。** `derive.py` の `FAMILY_PROFILES` の先頭行（R&B / Soul / Funk）は今日の定数そのものです。これは辻褄合わせではなく、**全ジャンルがひとつのジャンルの数値を受け取っていた**という事実を書き出したものです。

データベースは名前・別名・BPM帯・拍子・mood tags・地域しか持っていません。密度もarticulationも入っていない。そこで `mood_tags` から密度を導いて「データから出した」と言うことはせず、`ableton.py` の `LIVE_GENRE_BY_FAMILY` と同じく**手書きのファミリー単位テーブル**にしています。テーブルに無いファミリーは推測を受け取らず、**従来の定数のまま**です。

**`drums.hat_density` は解凍していません。** `composer.py` が 0.3 の閾値で8分/16分の2値に量子化しているため、0.4 と 0.9 は同じMIDIを生みます。ここで動かしても変わるのはプロンプトの文言だけで、制御しているように見えて制御していない状態になります。composer側の連続化は別の変更です。

## ジャンル認識（Music Genre Master Database）

`MusicBrain` のジャンル認識は、同梱のジャンルデータベース（`src/kihachi_music_ai/data/genres.json`、1020ジャンル・37ファミリー）が担います。

以前は `mutation_funk` / `dub` / `tech_house` の3つを手書きの規則で拾い、**それ以外を全て `electronic` に潰していました**。ボサノヴァもドラムンベースもシューゲイザーも同じ値になり、AbletonGPT境界で `edm` に変換され、909のドラムマシンキットが割り当てられていました。下流をいくら詳しくしても、入口で区別が消えているため回復できません。

```bash
python3 tools/build_genre_data.py Music_Genre_Master_Database_v0.2.xlsx
```

ワークブックを更新したらこれでJSONを再生成します（標準ライブラリのみ。表計算ライブラリは不要）。

* **英語名・カタカナ別名の両方**を照合します。実プロンプトはジャンル名をラテン文字で書くことが多いですが、「ボサノヴァ」「テックハウス」も引けます。
* **長い名前が優先**されます。`Tech House` は `House` を兼ねず、`Dubstep` は `Dub` になりません。
* **ファミリーより具体ジャンルが優先**されます。「ダブ」は `Reggae / Dub / Ska` ではなく `dub` です（前者だとダブセンドの判定が壊れます）。
* **日本語の語中一致を拒否**します。「ス**ラップ**ベース」の「ラップ」でHip-Hopを拾わないためで、日本語には語境界が無いため文字種の連続で判定しています。取りこぼしは許容し、誤検出を避ける方針です。

既存の3ジャンルの挙動は完全に保存されています。DBの名前はスラグ化すると旧名と一致するため（`Tech House` → `tech_house`）、swing値・ドラムパターン・ダブセンド・歌詞語彙の判定はそのまま動きます。

### DBの数値をSongSpecへ

認識だけでなく、DBが**実際に信号を持っている数値**をSongSpecへ流します。順序は「プロンプトの明示 → DB → 従来の定数」で、明示があれば必ずそちらが勝ちます。

| 入力 | 以前 | 現在 |
|---|---|---|
| `ドラムンベース。` | 120 BPM | **172.5 BPM** |
| `ダブ。` | 120 BPM | **75 BPM** |
| `Tech House。` | 120 BPM | **126 BPM** |
| `ボサノヴァ。` | 120 BPM | 120 BPM（後述） |

**BPMは範囲が狭いときだけ使います。** v0.2の実測でBPM範囲の中央値は100幅もあり、大半はファミリーからの継承です。「70〜180」は速度ではなく速度の不在で、その中点を採用するのは推測をデータに見せかける行為になります。幅40以下の162ジャンルだけが対象で、それ以外は従来どおり120に落ちます（`Ambient` の `bpm_min=0` のような不正値も除外）。

`mood_tags` は `darkness` と `psychedelic` に流します。従来はこの2つが全ジャンルで同じ定数でした。darknessは「暗↔明」の比の加重平均、psychedelicは**総重みで正規化**します（重み0.3のダブ1つで曲全体が1.0にならないように）。どちらの軸についてもタグが何も言っていない場合は `None` を返し、呼び出し側は0.5に引き寄せられず従来の定数を保ちます。

**`meter` は使っていません。** v0.2で曖昧さのない単一拍子は `4/4`（476件）だけで、それは既定値と同じです。実装しても死にコードになるため、拍子が個別化されるまで見送ります。

## 使いながら学習する（編集を既定値へ還元する）

`edit` の指示は、既に**定量化された訂正**として記録されています。`applied_spec_edit.json` に `{"path": "mutation", "from": 0.88, "to": 1.0}` が、隣の `song_spec.json` にそのときのジャンルが残っている。同じ訂正が繰り返されるなら、それは編集ではなく**最初からそうであるべき既定値**です。

```bash
python3 -m kihachi_music_ai learn projects --out preferences.json
python3 -m kihachi_music_ai compose '...' --preferences preferences.json
```

```
- observations: 60
- priors: 28  fingerprint: 9b5badfd20804959
    dub                    fx_amount    n=4   offset +0.169
    family:House           fx_amount    n=4   offset +0.169
```

これはコーパス収集で解けなかった問題への回答です。FMAとAcousticBrainzの両方で実測した結果、**ジャンルはテンポを説明しません**（分離度0.26、ジャンル内の広がり約35BPM）。公開音源をいくら集めても「このダブのブレイクダウンはどうあるべきか」は出てきません。本人の編集履歴なら出ます。ライセンスもオクターブ誤りもジャンル体系の不一致もありません。

### 決定論を壊さないこと

KIHACHIは「同じseedなら同じ曲」を保証し、repaintプランは `song_spec_sha256` に固定され、MIDIのバイト一致を回帰テストで縛っています。学習値が黙って効くと、この3つが同時に壊れます。

そこで**明示的に渡したときだけ**適用します。`learn` が priors をバージョン付きファイルへコンパイルし、`--preferences` を付けたときに限り効く。「同じseed＋同じpreferences＝同じ曲」は保たれ、差分も巻き戻しもできます。指定しなければ出力は従来と1バイトも変わりません。

priorsが実際に効いた曲は、`song_spec.json` に `preferences_fingerprint` を残します。seedだけではもうその曲を特定できないからです。効かなかった曲にはこのフィールド自体が出力されず、既存のSongSpecのバイト列と `song_spec_sha256` はそのまま保たれます。

### 縮小推定であって学習ではありません

編集は数十件のオーダーです。この規模ではニューラルネットではなく**計数と縮小**が正しい推定です。

```
offset = mean_delta × n / (n + 4)
```

n=1なら平均の20%、n=9で69%しか動きません。**編集の大半はその曲固有の判断**であって恒久的な好みではないので、1件の訂正が規則に昇格しない設計にしています。反対向きの訂正は打ち消し合います。

証拠が足りないジャンルは、DBの階層を**ジャンル → ファミリー → 全体**と遡ります。tech house単独で学習に足る編集数は集まりませんが、Houseファミリーなら集まります。1020ジャンルの階層がここで効きます。

## Liveの楽器割り当て

`ableton-plan`は、新しく作るBass／Sub／Chords／Synth／Arpトラックへ
`apply_live_instrument_selection`を出力します。KIHACHIが渡すのはパートの役割と
SongSpecから導いたgenre・moodだけです。OperatorなどのLive固有デバイス名と、
インストール状況に応じたフォールバック順はAbletonGPTが管理します。

実行時は既存デバイスを先に読み、候補と一致する楽器が1台だけなら再開として受け入れます。
別の楽器がある場合は置換せず拒否します。

ドラムは別の操作を使います。空のDrum Rack／Impulseは挿入に成功したうえで**無音**なので、
デバイス挿入では終わりません。そこでDrumsトラックには `apply_live_drum_kit` を出力します。
KIHACHIが渡すのはここでも意図だけ（`track_index`／`role`／`genre`／`mood`）で、
プリセット名も `.adg` もブラウザのパスもLive URIも一切書きません。実在するキットの候補・
候補順・ブラウザ上の位置・読み返し検証はすべてAbletonGPTが所有し、適用時にLiveの
ブラウザを走査して解決します。

`--split-drums` でキットを3トラックに分けた場合は、kick／snare／percussionのそれぞれに
1台ずつキットをロードします。インストゥルメントの無いトラックは、クリップが何音しか
鳴らさなくても無音だからです。percussionだけは専用のパーカッションキットを優先します。

Vocoderは引き続き例外です。carrier／modulatorの配線が必要なので、単体楽器を挿しただけで
完成扱いせず、警告を出します。

## センド（ダブのFXをリターンへ送る）

```bash
python3 -m kihachi_music_ai ableton-plan projects/my-song --send chords:1:0.1:0.6
```

`send_index` の0がリターンA、1がB。どのリターンかはLiveセット側の事実なので導出できません
（AbletonGPTの`get_mix_snapshot`が名前を返します）。

SongSpecの`fx_amount`は、各セクションの開始拍・長さ・送り量を持つ
`set_clip_send_envelope`へ変換されます。ダブのディレイ・スローは、曲全体の
平均値ではなく、セクションごとに動きます。

```
- send envelope: track 4 → return B, 9 steps (0.250-0.600)
```

LiveはArrangement上のオートメーションを直接書けないため、順序は
**Sessionクリップ作成 → Send Envelope → Arrangementへコピー**です。

Liveは同じpitch・同じstartのノートを1音へ統合し、同じpitchのノートが
重なると前のdurationを次のstartまで短縮します。`ableton-plan`はこの規則を
先に適用し、統合・短縮した件数をwarningへ出すため、計画のノート数とLiveへ
保存されたノート数が一致します。

## Revision Loop（測る→直す→また測る、を回す）

`analyze`で測り、`review`で直す場所を決め、`stage-repaint`で新しいプロジェクトを作り、
`render`で埋める。この4手を1ラウンドとして自動で回します。

```bash
python3 -m kihachi_music_ai revise projects/my-song --rounds 3 --base-url http://127.0.0.1:8001
```

まず何をするか見るだけなら、生成せずに確認できます。

```bash
python3 -m kihachi_music_ai revise projects/my-song --dry-run
```

ラウンドは1回ごとにレンダーを伴い、CPUでは数分かかります。途中で失敗しても
`revision_log.json`はラウンドごとに書かれるので、測り終えたテイクの記録は残ります
（`execution_state`が`failed`になり、`stopped_because`に原因が入ります）。

```bash
python3 -m kihachi_music_ai revise projects/my-song --resume
```

`--resume`は、既に音声があるラウンド（`-revNN`）を再レンダーせずに測り直して続きから進めます。
音声の無い中途半端なディレクトリは、`--resume`を付けても拒否します。
既存の`revision_log.json`がある状態で新規実行すると、以前の履歴を守るため停止します。
続行なら`--resume`を使い、最初からやり直す場合は既存ログとラウンドを退避してから実行します。
各ラウンドは`repaint_plan.json`の改訂文、strength、latent/waveform crossfade、mask modeをそのままACE-Step requestへ渡します。

人に共有する要約も各ラウンド後に残す場合は、Markdownの保存先を明示します。

```bash
python3 -m kihachi_music_ai revise projects/my-song \
  --rounds 3 \
  --revision-log-markdown projects/my-song/revision_log.md
```

`revision_log.json`が機械可読な正本で、Markdownは同じ状態の共有用要約です。
どちらも原子的に置換され、途中でレンダーが失敗した場合も、測定済みテイクと失敗理由を
`execution_state: failed`として残します。`--dry-run`はrevision loopを開始しないため、
`--revision-log-markdown`とは同時に指定できません。既存ファイルは上書きせず、失敗した
revisionを`--resume`する場合だけ、KIHACHIが以前に書いたMarkdownログを更新できます。

**候補は自動採用しません。** 各ラウンドは隣に新しいプロジェクトを書き、元のプロジェクトの
入力には一切触れません（書き戻すのは`revision_log.json`と、明示した場合の共有Markdownだけです）。最後に順位付きで並べて終わります。順位は「blockingな欠陥がないもの」が先、
その中で整合度順です。整合度88.69の`aligned`なテイクに2.28秒の無音が空いていた例があるため、
穴の空いたテイクは点数では勝てません。

採用を機械が決めない理由は、整合度スコアが**SongSpec通りかを測るだけで、良し悪しを聴けない**
ことにあります。同じ設定でseedを変えるだけでこのスコアは33点動きました。

停止条件は3つ。直す場所が無くなる、ラウンド上限、または整合度の伸びが1.00点未満です。
最後の閾値が0でないのは、seedのばらつきがそれより桁違いに大きく、
0.1点の改善は結果の顔をしたノイズだからです。

## 候補比較（聴いて選ぶためのページ）

`revise` は「聴いてから選べ」と言って終わりますが、聴く手段を持っていませんでした。

```bash
python3 -m kihachi_music_ai report projects/my-song --from-revision-log
```

`candidates.html` を書きます。各テイクを再生でき、波形にセクション境界と欠陥の位置が重なります。
音声は埋め込まず相対リンクです（1テイク13MB、3本入れたらページではなくなります）。
プロジェクトを丸ごと移動してもリンクは切れません。

各テイクに指示欄があります。文章を書くと、そのまま貼って実行できる`edit`コマンドを組み立てて表示します
（アポストロフィや`$`、`;`を含む文もPOSIXの単一引用で正しく囲みます）。

ページ自体は生成も採用も削除もしません。組み立てるだけで、実行は明示的な行為のままです。順位付けの規則は`revise`と同じで、
blockingな欠陥のないテイクが先、その中で整合度順です。

**「未スキャン」と「欠陥なし」は区別します。** 測っていないことを「問題なし」と表示するのは、
検査の欠落を結果に見せかけることだからです。

試聴後の判断は、音声を動かさずに明示的に記録します。

```bash
python3 -m kihachi_music_ai decide projects/my-song \
  --also projects/my-song-rev01 \
  --selected projects/my-song \
  --reason "Base維持。改訂版よりグルーヴが自然だった"
```

`decision_log.json`へ、選んだプロジェクト、比較した全候補、各AudioのSHA-256、整合度、
素材検査状態、試聴理由を追記します。Baseを選んだ場合は`retain_base`、別候補なら
`select_candidate`です。このコマンドは選択を**記録するだけ**で、Audioのコピー、置換、
削除、名前変更は行いません。判断を変更した場合も以前のentryは消さず、次のentryを追記します。
`report`は選択時のSHA-256と現在のAudioを再照合し、差し替わっていれば`changed`、
見つからなければ`missing`と表示して、古い試聴判断を現在のファイルへ流用しません。

## stem分離（v0.2）

**KIHACHIはstemを作りません。**作る道具（Demucs等）はtorchと数百MBの重みを要求し、
コアは`dependencies = []`のままにしたいからです。代わりに**契約**だけを持ちます —
どこへ何という名前で置くか、何を検証するか、何を記録するか。詳細はADR-0008。

```bash
# 走らせるべきコマンドを表示する（分離はしない）
python3 -m kihachi_music_ai stems prepare projects/my-song
```

```
- run this yourself, wherever the separator lives:
    demucs -n htdemucs --filename {stem}.{ext} -o projects/my-song/audio/stems projects/my-song/audio/ace-step-01.wav
- then take the result in:
    python3 -m kihachi_music_ai stems import projects/my-song
- nothing written
```

表示されたコマンドをローカルCPUで走らせてもGPUの箱で走らせても構いません。Demucsは`-o`の下に
必ずモデル名のディレクトリを掘るので（`--filename`が変えるのは葉の名前だけで、この階層は消せません）、
実際の配置は`audio/stems/htdemucs/{drums,bass,other,vocals}.wav`になります。平置きで出す分離器も
あるため、取り込み側は**両方**を見ます。

```bash
python3 -m kihachi_music_ai stems import projects/my-song
```

取り込み時に**尺**とchannel数が元Audioと一致するかを検証します。**尺がずれたstemは拒否します** —
小節グリッド上の解析を静かに狂わせるより、ここで止めるほうが安いためです。

sample rateの一致は求めません。htdemucsは48 kHzを渡しても44.1 kHzで返します（2026-08-15実測、
尺は122.1820 sで完全一致）。解析器は各ファイル自身のrateを読むので測定に影響しません。
事実として`resampled`にだけ残します。

`stem_manifest.json`に元AudioとstemのSHA-256、モデル名、尺、sample rateが残ります。
元Audioもstemも書き換えません。

分離済みstemは通常のWAVなので、解析経路は新設していません。

```bash
python3 -m kihachi_music_ai analyze projects/my-song --audio audio/stems/htdemucs/other.wav
```

stemの解析は`audio_analysis.other.json`と`material_defects.other.json`へ書かれます。
**テイク自身の`audio_analysis.json`は上書きしません** — stemを測るたびに元の測定が消えるのは、
この経路を最初に通したときに実際に起きた事故です。

### 測ってみた結果: マスキングではありませんでした

2026-08-15、2テイクをhtdemucsで分離し、ハーモニーを担う`other` stemを測りました。

| | 2分テイク（D# minor） | 32小節テイク（D# major） |
|---|---|---|
| coverage（ミックス→stem） | 0.4286 → **0.8036** | 0.5000 → **0.6562** |
| キー確信度（ミックス→stem） | 0.1151 → **0.4249** | 0.1652 → **0.2746** |
| コード進行一致率 | 0.0 → **0.0** | 0.0 → **0.0** |

**stemでは検出器が自信を持ち、その上で「進行は一致しない」と言います。**
マスキングでは説明がつきません。v0.1が「区別できない」と書いた2択のうち、
**マスキング説は否定されました**。

送信内容に誤りはありません。32小節テイクの`ace_step_request.json`は`key_scale`に`D# major`、
プロンプト本文にも`in D# major`、進行も`D# - Cm - G# - A#`を載せています。

**外れ方はテイクごとに違います。**

- 2分テイク: 45の信頼小節でF#mが23回。**ほぼ静止したハーモニー**で、4小節周期になっていません。
  検出されたF#・C#・A#m・G#mはD#短調の音階和音なので、キー自体は概ね合っています。
  5度上への移調も疑いましたが、根音33.3%に対し質は4.4%しか合わず、移調では説明できません。
- 32小節テイク: **三全音上で根音も質も52.4%一致**します（D#→A、Cm→F#m）。
  キー推定もA majorで、検出された和音の根音はどれもD#長調の音階外です。
  こちらは「進行の形は保ったまま、別のキーで鳴っている」ように見えます。

共通するのは**指定した進行が指定した小節で鳴っていない**ことだけで、その外れ方に一貫性はありません。
2テイクとも同じ生成器・同じ設定なので、これを一般則として読むのは早計です。

残る留保: ディレイの深い素材でメジャー/マイナーの判別は1音差でしかないこと。
ベースstemは単音ラインで三和音を作らないため、三和音前提の推定器では裏付けが取れないこと
（coverage 0.23）。低域偏重の出所は未測定です。

## v0.1の既知の限界

**いずれも実測で確認した仕様上の限界であり、不具合ではありません。**「直そうとして時間を使わないため」に集めています。
詳細は各節にあります。

| 限界 | 実測 | 回避策 |
|---|---|---|
| コード進行がミックスから測れない | 29テイク中18テイクで一致率0.0、残りも最大0.167 | `midi-review`で照合する（MIDIは設計から書いているので推定不要）。**検出器のバグではありません** — 合成した進行を通すと一致率1.0 |
| 構造タグ`[inst]`と散文の`no vocal`が無視される | 2経路で指示して両方とも不通 | セクションを歌詞なしで`repaint`する。対象は`instrumental-plan`が出す |
| 指定した和音進行・キーが鳴らない | stem上で検出器が自信を持っても一致率0.0（2テイク）。request・プロンプト双方に正しく載っている | 現状なし。`midi-review`でKIHACHI側の設計が正しいことは確かめられる |
| turboが`inference_steps`を無視する | steps 8と60で音声がバイト単位で同一 | `seed`は効くので、変化が欲しければseedを変える |
| 短い曲でtail guardが届かない | 32小節ではguard 2小節で2.32s不足（blocking）、4小節でも1.31s | `--tail-guard-bars`を4以上にする、または`trim-tail`で後から落とす。56小節では既定で足りる |
| repaintがクリックを消さず移動させることがある | 17秒テイクでは消失、2分テイクでは61.55s→63.73sへ移動（いずれもマスク内） | 測定値だけで追加レンダーを決めない。閾値付近（0.5前後）は可聴か先に聴く |
| 低域に寄る | 6kHz以上が全エネルギーの1〜2%。31テイクの中央値は21.9で、40超えが7件 | 較正は2026-08-15に再測して据え置き。20秒未満のテイクでは比が420〜660まで跳ねるが、これは尺の性質（イントロ主体で高域が育たない）であってミックスの問題ではない |
| True peak未実装 | — | インターサンプルピークにはオーバーサンプリングが必要なため見送り |

コード進行と低域の2つは、原因の切り分けにstem分離が要ります（v0.1の範囲外）。

## v0.1の境界

- Music Brainはルールベースで、BPM、キー、ジャンル、質感、演奏指示を決定的に解釈します。
- MIDIはSMF Format 0、480 PPQです。DrumsはGeneral MIDIのChannel 10を使います。
- `prompt.txt` は音声生成器へ渡す中立的なテキストです。ACE-Step 1.5 RESTアダプターが、この内容とSongSpecのBPM・キー・尺を公式API形式へ変換します。
- `prompt.json` は同じプロンプトを機械可読にしたものです。BPM・キー・拍子・尺・進行・セクション・パートと、コンパイル元SongSpecの `song_spec_sha256` を持ちます。生成器固有のパラメータ（`inference_steps` など）は入っていません — それらは `ace_step_request.json` の担当です。レンダラーがまだ繋がっていない段階でも、この1ファイルで曲の設計を渡せます。
- `prompt.json` は手で編集して読み戻せます。プロンプト本文を書き換えて渡すと、SongSpecから再コンパイルせずそのまま使われます:

```bash
uv run kihachi ace-step prepare projects/my-song --from-brief prompt.json
uv run kihachi ace-step render  projects/my-song --from-brief prompt.json
```

  SongSpecと食い違うブリーフ（プロンプト・尺・シードのいずれか）を渡すと、どこが違うかを表示したうえでブリーフ側を採用します。`render` の場合、テールガードの切り戻し先はブリーフ自身の `total_bars`・`bpm`・`time_signature` から求めた長さです（SongSpec側のグリッドに切り戻すと、尺を書き換えたブリーフはまさにその部分を失います）。どのブリーフでレンダーしたかは `ace_step_result.json` の `render_brief`（パス・SHA-256・SongSpecとの一致）に残ります。
- Audio-to-MIDI、stem分離、複数候補の音楽的な自動採用、Ableton Live展開、LLM接続は次段階です。ACE-StepのAudio-to-Audioは構造保持用の`cover`と範囲再生成用の`repaint`に対応しています。

## ACE-Step 1.5アダプター

ネットワーク接続なしで、送信予定の内容を確認できます。

```bash
python3 -m kihachi_music_ai ace-step prepare \
  projects/mutation-signal
```

`ace_step_request.json`にはAPIキーを含めません。既定では、KIHACHIのSongSpecをACE-Step側で書き換えないように`thinking=false`、`use_format=false`、CoT補完も無効です。

### turboモデルは`inference_steps`を無視します

`acestep-v15-turbo`に対しては、`--inference-steps`が**出力を一切変えません**。2026-08-13に対照実験で確認しました。プロンプト・歌詞・seedを固定し`inference_steps`だけを8と60にして`/release_task`へ直接投げたところ、別タスクとして別々に生成されたにもかかわらず（task_idも出力ファイルのUUIDも別）、音声は**バイト単位で同一**でした。

| | task_id | 生成時間 | 音声SHA-256 |
|---|---|---|---|
| steps=8 | `0c81d668…` | 1.73s | `3d32c369…` |
| steps=60 | `351afb24…` | 1.79s | `3d32c369…` |

60ステップが1.79秒で終わること自体が、ステップ数が使われていない証拠です。キャッシュではありません。seedを変えれば出力は変わるので、**seedは効き、stepsは効きません**。

蒸留済みのturboモデルが固定ステップ数で動く設計なら、これは仕様として筋が通ります。非turboモデルでどうなるかは未確認です（検証したインスタンスにはturboしか入っていませんでした）。

したがって`ace_step_request.json`の`inference_steps`は、turbo相手では**記録以上の意味を持ちません**。品質が足りないときにここを上げても何も起きないので、seed・プロンプト・モデルの側で対処してください。

### 最初の生成の前にモデルを初期化してください

モデル未ロードの状態で生成を投げると、初回の初期化待ちでクライアントが先に切れます。2026-08-12の実機スモークはこれで中断し、`task_id`を回収できないまま音声だけをリカバリする羽目になりました（`ace_step_recovery.json`がその記録です）。

```bash
curl -X POST http://127.0.0.1:8001/v1/init \
  -H 'Content-Type: application/json' \
  -d '{"model":"acestep-v15-turbo","init_llm":false}'
```

完了まで実測で約50秒。`/health`の`models_initialized`が`true`になってから`render`を実行すれば、この失敗は起きません。

ACE-Step 1.5 RESTサーバーを起動後、生成と音声取得を実行します。

```bash
ACESTEP_API_KEY='serverに設定したキー' \
python3 -m kihachi_music_ai ace-step render \
  projects/mutation-signal \
  --base-url http://127.0.0.1:8001
```

認証を使わないローカルサーバーなら、`ACESTEP_API_KEY`は不要です。キーは環境変数からだけ読み、リクエストJSON、結果JSON、CLI出力には保存しません。

生成成功後は次を追加します。

```text
ace_step_request.json
ace_step_result.json
audio/ace-step-01.wav
```

ACE-Step側の5Hz LM計画を使う場合だけ`--thinking`を追加します。モデル名はサーバーの`/v1/models`で確認した値を`--model`へ渡します。

### Audio-to-Audio（cover / Remix）

既存Audioの尺と大きな構造を保持しながら、SongSpec、改訂指示、LoRAを使って再生成できます。Mac上のAudioはmultipartで直接アップロードされるため、Vast側へ手動コピーしたり、サーバーの絶対パスを成果物へ保存したりしません。

```bash
python3 -m kihachi_music_ai ace-step render \
  projects/mutation-signal-cover \
  --base-url http://127.0.0.1:8001 \
  --task-type cover \
  --source-audio projects/mutation-signal/audio/ace-step-01.wav \
  --audio-cover-strength 1.0 \
  --cover-noise-strength 0.8 \
  --revision-file projects/mutation-signal-cover/revision_prompt.txt \
  --lora-path /workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final \
  --lora-scale 0.8
```

`cover_noise_strength`は0.0で新規生成寄り、1.0で参照元に最も近くなります。生成結果の`ace_step_result.json`には、参照Audioのファイル名、SHA-256、サイズ、cover強度を記録します。ローカル絶対パスとAudio本体はJSONへ保存しません。

### 範囲再生成（repaint）

`repaint`は参照元の指定範囲だけを再生成し、それ以外を保持します。推奨指定はSongSpecのセクション名です。CLIがBPMと拍子から安全に秒へ変換します。

```bash
python3 -m kihachi_music_ai ace-step render \
  projects/mutation-signal-repaint \
  --base-url http://127.0.0.1:8001 \
  --task-type repaint \
  --source-audio projects/mutation-signal/audio/ace-step-01.wav \
  --repaint-section psychedelic_drop \
  --repaint-mode balanced \
  --repaint-strength 0.65 \
  --cover-noise-strength 0.0 \
  --chunk-mask-mode explicit \
  --repaint-latent-crossfade-frames 10 \
  --repaint-wav-crossfade-sec 0.25 \
  --revision-file projects/mutation-signal-repaint/revision_prompt.txt \
  --lora-path /workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final \
  --lora-scale 0.8
```

小節で選ぶ場合は`--repaint-bars 25:32`を使います。小節番号は1基準で両端を含みます。従来どおり`--repainting-start 52.364 --repainting-end 69.8`という秒指定も使えますが、セクション/小節指定とは同時に指定できません。

`conservative`は参照元保持を優先し、`aggressive`は指定範囲の新規生成を優先します。`balanced`では`repaint-strength`で両者を調整します。手動範囲では`chunk-mask-mode explicit`を使います。repaintの`cover-noise-strength`は通常0.0にします。高くすると、内部で消去されたマスク領域の無音へ寄る場合があります。結果JSONには選択したセクション/小節、編集開始・終了、mode、strength、mask mode、クロスフェード、参照元SHA-256を保存します。

### Tail guard（最終小節の無音対策）

ACE-Stepは与えられたバッファの中で「曲を完結」させます。曲尺ぴったりの長さを要求すると、モデルはバッファ終端より手前で終止を書き、最後の1小節ほどが無音の残り（outro）になります。このプロジェクトのseed 8では、69.800秒のバッファに対して音楽が67.504秒で止まり、末尾2.296秒が無音でした。これがbaselineとauto-01の両方でbar 32が`normalized_energy 0.0`（RMS約-63 dBFS）になっていた原因です。プロンプトでは直せません。バッファ長の問題だからです。

`--tail-guard-bars`は曲尺より数小節だけ長いバッファを要求し、モデルの終止を採点対象の小節の外へ追い出します。生成後、配信WAVは曲尺へトリムし直され、未トリムのレンダーは`audio/ace-step-01.untrimmed.wav`として監査用に残ります。トリム端には10 msのフェードだけを掛けるので、クリックは出ず、小節エネルギーは動きません。

```bash
python3 -m kihachi_music_ai ace-step render \
  projects/mutation-signal-repaint \
  --base-url http://127.0.0.1:8001 \
  --task-type repaint \
  --source-audio projects/mutation-signal/audio/ace-step-01.wav \
  --repaint-bars 29:32 \
  --tail-guard-bars 2 \
  --cover-noise-strength 0.0 \
  --chunk-mask-mode explicit
```

repaint範囲が最終小節に届く場合は、マスク自体もguard領域まで伸ばします（モデルはマスク終端で終止を書くため）。範囲が曲中で終わる場合、guardは範囲に影響しません。`--tail-guard-bars`はWAV専用で、0〜8小節の範囲です。結果JSONの`tail_guard`ブロックに、要求尺・曲尺・トリム前後のSHA-256・実測の`delivered_music_end_sec`が残ります。

`review`は既定でtail guard 2小節を計画へ入れます（`--tail-guard-bars 0`で無効）。また、セクション全体ではなく問題小節だけを狙う**bar-level候補**も出します。最終小節がセクション目標より0.25以上落ちていれば、その連続範囲を最低4小節まで広げた候補（例: bars 29:32）を`bar_level_candidates`へ記録し、セクション平均が目標付近（誤差0.05以内）なら`recommended_selector`を`bars`にします。`--prefer-bar-level`を付けると、その狭い範囲が実際の`selection`になります。

### turboでのtail guardは効きますが、短い曲では足りません

tail guardは「余分に生成させて、後で曲グリッドまで切り戻す」設計です。**モデルが長いバッファを実際に使うことが前提**になります。

2026-08-13には、74.182s（2小節分のguard込み）を要求した生成が69.80sで返り、`tail_guard`ブロックは`source_frames == kept_frames` — **切り戻す余地がゼロ**でした。ここから「turboでは常にblocking」と書いていましたが、**2026-08-15に同条件で再測して再現しませんでした**。同じ32小節・seed 8・turboで74.182sを要求し、**74.2sが全量返り、69.818sへの切り戻しも成功**しています。配信が頭打ちになる現象は、あの日固有のものだったと考えられます。

再測で分かった本当の性質は、**必要なguard幅が曲尺によって変わる**ことです。モデルはバッファ終端の手前で終止を書きますが、その「手前」の量が一定ではありません。

| 曲尺 | guard | 要求尺 | 音楽終端 | グリッドまでの不足 | `silent_gap` |
|---|---|---|---|---|---|
| 56小節 (122.182s) | 2小節 | 126.546s | 122.1775s | 0.005s | 出ず |
| 32小節 (69.818s) | 2小節 | 74.182s | 67.50s | 2.32s | **blocking** |
| 32小節 (69.818s) | 4小節 | 78.545s | 68.51s | 1.31s | warning |

**2分の曲では既定の2小節で足ります。** 32小節では足りません。またguardを2小節（+4.36s）足しても音楽終端は1.01sしか後ろに動きません（追加分の約23%）。モデルは終止をずらすのではなく、与えられたバッファ全体で構成を組み直すためです。この比率だと32小節でグリッドに届かせるには6〜7小節のguardが要る計算になり、そのぶんGPU時間も伸びます。既定を一律に上げていないのはこのためです。

短い曲でblocking判定が出た場合は、`--tail-guard-bars`を4以上に上げる（上限8）か、次の`trim-tail`で後から落としてください。repaintでも縮みはします（4.80s → 2.02s、alignmentは77.13 → 90.10 / `partial` → `aligned`）が、消えません。原因が生成尺そのものにあるためです。

### `trim-tail`: 生成後に末尾無音を落とす

guardで届かなかったぶんは、後から切れます。

```bash
python3 -m kihachi_music_ai trim-tail projects/my-song --dry-run
python3 -m kihachi_music_ai trim-tail projects/my-song
```

```
- music ends at 67.77 s of 69.80 s; keeping 68.02 s (+0.25 s pad)
- removes 1.78 s below -40 dBFS
- now 1.80 s (0.826 bars) short of the 69.82 s song grid
```

**レンダーそのものには触れません。**`audio/ace-step-01.tail-trimmed.wav`を隣に書き、元ファイルはそのまま残します — モデルがどう振る舞ったかの証拠だからです。監査記録は`tail_trim.json`へ。

最後の可聴サンプルから`--pad`（既定0.25秒）だけ残すので、リバーブの減衰を切り落としません。除去量が0.5秒未満なら**切らずに拒否**します（blocking閾値未満なら既に許容範囲です）。閾値を一度も超えない音声も拒否します — 切って「欠陥解消」にするのは、レンダー全体を消すのと同じだからです。

**切ると曲グリッドより短くなります。**69.818sの曲が68.02sで出るので、小節数と合わなくなります。これは丸め誤差ではないため、`tail_trim.json`に秒と小節の両方で記録し、コマンドも毎回表示します。

実測では、この処理で`material_defects.json`が`blocking 1` → `clean: true`になります。

#### `review`と`revise`が末尾無音を見分けます

手動で気付く必要はありません。**末尾まで続くblocking無音**を検出すると、`review`が修正手段を名指しします。

```
- material blocking: 2.02 s below -50 dBFS starting at 67.78 s
- silent tail: 2.02 s runs to the end; a repaint cannot remove it -- run `trim-tail`
```

`revise`も同じ判定でループを止めます。repaintは配信された尺を縮められないので、末尾無音は何ラウンド回しても残ります（実測で4.80s → 2.02sまでで頭打ち）。**GPUを1レンダー分無駄にしないための停止**です。停止理由は`revision_log.json`へ記録されます。

曲の**途中**の無音は別問題として扱い、従来どおりrepaintの対象に残します。そちらはモデルが実際に書き直せる素材だからです。

## チャンク分割レンダー

9セクションの編成を1つのプロンプトで通すと、曲の3分の1を過ぎたあたりで自分の計画を無視し始めます（計画境界の再現率 1.0 → 0.25、セクションエネルギー相関 0.75 → 0.34）。そこで曲をセクション単位のチャンクに区切り、**各チャンクを自分のセクションだけを述べたプロンプトでレンダー**します。最初のパスが全長のベッドを敷き、以降は直前のレンダーを参照元とする自分の範囲のrepaintです。

```bash
uv run kihachi ace-step plan-chunks projects/my-song --target-chunk-bars 32
uv run kihachi ace-step render-chunks projects/my-song --base-url http://127.0.0.1:8001
```

`chunk_plan.json`は手で編集する前提のファイルです（チャンク幅、repaint強度、クロスフェードは実際に回したくなるつまみです）。読み込み時に**全小節がちょうど1回ずつ描かれること**を検証し、隙間・重なり・順序の乱れ・曲末に届かない計画を、どこで途切れているかを示して拒否します。隙間はベッドがそのまま残る区間になり、レンダーログ上は成功と見分けが付かないためです。

チャンクは1つあたりCPUで数分かかります。途中で失敗した場合、完了済みのチャンクは`chunks/`に残り、`chunk_render_log.json`には`execution_state: incomplete`と何番まで終わったかが記録されます。

```bash
uv run kihachi ace-step render-chunks projects/my-song --resume
```

`--resume`は完了済みのチャンク（音声と結果JSONが揃っているもの）を再利用し、残りだけをレンダーします。再利用したステップはログに`reused_from_previous_run`として残ります。音声だけあって結果JSONが無いステップは、途中で切れたダウンロードの可能性があるため再レンダーします。

## KIHACHI LoRA

LoRAは生成JSONへ埋め込むのではなく、ACE-Stepサーバー上のモデルへロードします。まず現在の状態を確認できます。

```bash
python3 -m kihachi_music_ai ace-step lora status \
  --base-url http://127.0.0.1:8001
```

KIHACHI LoRAをロードし、強度を設定して有効化します。

```bash
python3 -m kihachi_music_ai ace-step lora load \
  /workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final \
  --scale 0.8 \
  --base-url http://127.0.0.1:8001
```

`lora_path`はMac側のバックアップ場所ではなく、ACE-Stepサーバーから読めるパスです。Vastでは上記のような`/workspace/...`を指定します。

ロードから生成まで一度に実行する場合:

```bash
python3 -m kihachi_music_ai ace-step render \
  projects/mutation-signal \
  --base-url http://127.0.0.1:8001 \
  --lora-path /workspace/ACE-Step-1.5/output/KIHACHI_LORA_v1/final \
  --lora-scale 0.8
```

このコマンドは、既存成果物の上書き可否を先に確認してから`load → scale → enable → status検証 → 音声生成`を実行します。検証済みのLoRA状態は`ace_step_result.json`の`lora`へ記録されます。APIキーは従来どおり成果物へ保存しません。

状態変更だけを行うコマンド:

```text
ace-step lora scale 0.6
ace-step lora enable
ace-step lora disable
ace-step lora unload
```

複数LoRAを扱うACE-Step構成では、`load --adapter-name kihachi`と`scale --adapter-name kihachi`、または生成時の`--lora-adapter-name kihachi`を使用できます。
