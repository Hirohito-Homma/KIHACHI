# KIHACHI Music AI v0.1

自然言語の音楽指示から、共通設計図 `SongSpec`、3本の標準MIDIファイル、音声生成向けプロンプトを作る最小動作版です。

```text
User Prompt
    ↓
Music Brain
    ↓
SongSpec
    ├── MIDI Composer → bass.mid / drums.mid / chords.mid
    └── Prompt Compiler → prompt.txt
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
```

出力先を指定する場合:

```bash
python3 -m kihachi_music_ai compose '...' --output projects/my-song
```

既存の対象ファイルは上書きしません。意図して再生成するときだけ `--overwrite` を付けます。

## Lyrics（歌詞を「文章」ではなく「パート」として書く）

設計書が立てている区別をそのまま実装しています。**文学的に良い歌詞**と**音楽的に使いやすい歌詞**は別物です。Vocoderは文章を欲しがりません。キャリアを通して4小節ごとに繰り返されても成立する2〜3語の命令形を欲しがります。

そのためライターはまず**声の処理方法**で駆動され、テーマは二番目です。モードがフレーズ長・反復・フックの戻り方を決め、SongSpecのジャンルと気分が語彙を決め、`section.vocal_probability` がそもそもどこで歌うかを決めます。

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

### 未検証の点

出力書式はACE-Stepの角括弧構造タグ（`[verse]` / `[chorus]` / `[bridge]` / `[inst]`）を使っていますが、**この規約を今回のセッションで実サーバーに対して検証できていません** — 作業中にACE-Stepサーバー（127.0.0.1:8001）が停止したためです。実生成で確認するまでは書式は暫定と考えてください。

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

外部パッケージなしで基本テストを実行できます。

```bash
python3 -m unittest discover -s tests -v
```

## Critic の二経路（MIDI照合 と Audio解析）

Analyzerは**完成ミックスから**ハーモニーを推定します。この方式には原理的な上限があり、実測でもキー信頼度 0.11、コード一致率は全テイクで 0.0 のまま動きませんでした。キック・ベース・コード・ボーカル・dub delayが同じ帯域で重なるため、検出器には見えません。

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

`repaint_plan.json`は、各セクションのenergy差、コード一致率、判読可能なコード小節率、開始境界、終端energy失速を比較し、最も修正優先度が高いセクションを1つ選びます。選択小節・秒範囲、局所的な改訂文、安全なrepaint設定、元解析AudioのSHA-256を保存します。計画作成だけでは生成を開始しません。

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
- low/high ratio: 64.373 (corpus median 19.8), centroid 258 Hz
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

## センド（ダブのFXをリターンへ送る）

```bash
python3 -m kihachi_music_ai ableton-plan projects/my-song --send chords:1:0.1:0.6
```

`send_index` の0がリターンA、1がB。どのリターンかはLiveセット側の事実なので導出できません
（AbletonGPTの`get_mix_snapshot`が名前を返します）。

**送り量は曲全体で1つの値です。** ダブのディレイ・スローは本来セクションごとに送りを変える技法ですが、
Liveがクリップエンベロープを公開しているのは**デバイスチェーン上のパラメータだけ**で、
センドはミキサー側にあるため`set_clip_parameter_envelope`が届きません。

そのためSongSpecの`fx_amount`は平均され、**平坦化した事実が警告として出ます**。

```
warning: chords send is one level for the whole song: fx_amount runs 0.30-0.70
and was averaged to 0.49. Live exposes clip envelopes for device parameters only,
so a send cannot be automated per section from here
```

0.30と0.70が同じ0.49になることを、耳で気づかせないためです。

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

**候補は自動採用しません。** 各ラウンドは隣に新しいプロジェクトを書き、元のプロジェクトには
一切触れません。最後に順位付きで並べて終わります。順位は「blockingな欠陥がないもの」が先、
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

## v0.1の境界

- Music Brainはルールベースで、BPM、キー、ジャンル、質感、演奏指示を決定的に解釈します。
- MIDIはSMF Format 0、480 PPQです。DrumsはGeneral MIDIのChannel 10を使います。
- `prompt.txt` は音声生成器へ渡す中立的なテキストです。ACE-Step 1.5 RESTアダプターが、この内容とSongSpecのBPM・キー・尺を公式API形式へ変換します。
- Audio-to-MIDI、stem分離、複数候補の音楽的な自動採用、Ableton Live展開、LLM接続は次段階です。ACE-StepのAudio-to-Audioは構造保持用の`cover`と範囲再生成用の`repaint`に対応しています。

## ACE-Step 1.5アダプター

ネットワーク接続なしで、送信予定の内容を確認できます。

```bash
python3 -m kihachi_music_ai ace-step prepare \
  projects/mutation-signal
```

`ace_step_request.json`にはAPIキーを含めません。既定では、KIHACHIのSongSpecをACE-Step側で書き換えないように`thinking=false`、`use_format=false`、CoT補完も無効です。

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
