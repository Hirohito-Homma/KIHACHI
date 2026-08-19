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

## ブリーフのどこが読まれたか（v0.2）

**ブリーフは黙って無視されます。**`MusicBrain`には「これは使わなかった」と言う手段が
ありませんでした。語彙は**25 trait / 155表現**しかありません（`dark`〜`contrast`/`flat`の16個は2026-08-17追加）。

```bash
python3 -m kihachi_music_ai read-brief "アンビエント。110 BPM、D#m。きらびやかで高域中心、繊細。"
```

`example_output`の実ブリーフ6種に当てた結果、85文字のアンビエント・ブリーフは
**traitが0**でした。BPM・キー・尺・ジャンル名は拾われますが、
**音の中身を指定した文は全部届いていません**。

```text
  きらびやかで高域中心   -> nothing acted on this
  繊細                 -> nothing acted on this
  ベースは控えめで薄い    -> nothing acted on this
  パーカッションは軽く    -> nothing acted on this
```

2026-08-17に`bright`が入り、この1行目だけが届くようになりました
（`きらびやかで高域中心 -> trait:bright`、SongSpecの`darkness`はジャンル既定の
0.48から0.227へ）。**残る3行は変わっていません**。読まれた割合は40%から50%です。

`compose`も、読まれなかった文があれば一行で報告します。**曲は作られます** —
ブリーフが何か言っていて、こちら側に聞く語彙が無かった箇所に既定値が入るだけです。
「未対応」は「却下」ではありません。

### 節の一部しか読まれないこともあります

`ダブの32小節`は「ダブ」が反応するので節としては読まれた扱いですが、**覆われたのは29%**です。
`_total_bars`は`分`しか読まないので、**「32小節」「16 bars」と書いても読まれず**、既定の32に落ちます。
そこで、覆われた割合が半分以下の節は`partly_read`として名指しします。

### この計測が実害を1つ見つけました

`D#マイナー`の被覆率が33%だったのを追ったところ、**`parse_key`はカタカナを知らず、
`D#マイナー`はD# *major*として解釈されていました**。

出荷済みの3プロジェクト（`kihachi-api-002`、`turbo-duration-recheck-01`、`-02-guard4`）が
「D#マイナー」と書いて**メジャーで作られています**。ハーモニーもMIDIもACE-Stepへのプロンプトも
全部その上に乗っていました。`マイナー`/`メジャー`/`短調`/`長調`を読むようにしています。

### 同じreaderの反対側の実害

カタカナを知らなかったreaderは、次はカタカナを読みすぎていました。`_KEY_RE`の境界は
`(?![A-Za-z])`だけなので、**日本語の音楽用語がそのままキーになります**。
「Aメロは静かに」は**Aメジャー**、「Bメロで盛り上げて」はBメジャー、
「Eギター」「Gベース」「Bパート」も同じです。誰も書いていないキーが、
キーの話ですらない節から決まっていました。

ラテン文字側はもっと広く、いずれもこのデータベースにある**ジャンル名**です。
`G-Funk`はGメジャー、`D&B`はD、`A Cappella`はA、`EBM`は`re.IGNORECASE`が
`b`（フラット）まで畳んだ結果**E♭マイナー**。「Gファンク」はそれを日本語へ持ち込んでいました。

裸の文字（品詞を決める語を伴わない`A`）に境界を足しています。品質語（`マイナー`/`minor`）が
付いていればマッチの内側なので、`D#マイナー`も`in A minor`も`key of G`も従来どおりです。
`brief`は同じreaderを再実行して被覆率を出す設計なので、正規表現ではなく
`theory.key_matches`を読むように変えました。片方だけ直すと、
**「Aメロを読んだ」と報告しながら読まない**という逆向きの食い違いになります。

ジャンル名とaliasの1662件を毎回この readerに通す不変条件をテストにしています
（`test_no_genre_can_be_named_without_stating_a_key`）。aliasはデータなので
コードに触れずに増え、キーを名乗る名前かどうかは読んでも見えません。

方針（LLMに何をさせ、何をさせないか）はADR-0011にあります。

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
| 質 | 変態/mutation、シンコペ、**スウィング/シャッフル/跳ね**、密度/厚く、激しく/energy、ゴースト、オクターブ、スペース/抜いて、ディレイ/fx |
| 方向 | もっと/上げ/増やし ↔ 抑え/減らし/下げ/薄く |
| 拒否 | 無し/要らない/禁止/入れないで/使わないで、no/without/avoid |
| 強さ | 少し (0.1) / 既定 (0.2) / かなり (0.35) |

セクション名は識別子として扱い、走査前に除去します（`dub_breakdown` の中の "down" が減少語として誤認されるため）。ASCII語は語頭境界を要求します（"group" の中の "up" を弾き、かつ "densely" は "dense" にマッチさせるため）。

### スウィングは修正指示から動かせます（v0.2）

**「跳ね」と `swing` は `groove.syncopation` を動かしていました。**ブリーフ側は
`swung` traitが入って以来ずっとこれらを `groove.swing` として読んでいるので、
**同じ語が、頼むときと直すときで違うノブに届いていた**ことになります。
「スウィング」「シャッフル」に至ってはこちらの語彙に無く、指示ごと拒否されていました
（swing語48件すべてが「別のノブ」か「読めない」）。

`swing` を独立したqualityにして、語はブリーフ側の `TRAIT_WORDS["swung"]` をそのまま
使います。`syncopation` の語は「シンコペ/syncopat/うねら」に戻しました。

**この値だけ範囲が0〜1ではありません。** 0.5がストレート、0.667が三連スウィングなので、
0.2動かすと音楽ではなくなります。強さは**その範囲に対する割合**として読みます
（`PARAMETER_RANGE`）。範囲が1.0幅の他のパラメータは計算が一切変わりません。

```
- reading: swing increase by 0.2
    (song-wide): groove.swing 0.54 -> 0.572      ← 0.74 ではなく
- reading: swing refused, down to the low pole
    (song-wide): groove.swing 0.54 -> 0.5        ← ストレート。0.0 ではなく
```

### 拒否は「低い極」に着地します

**「ゴーストノートは無しで」は、ゴーストノートを0.34から0.54に増やしていました。**
減少語が1つも入っていないので方向が既定の「上げる」になり、指示と逆のことをしていました
— ブリーフ側が #82・#84 で直したのと同じ失敗が、こちら側にはそのまま残っていました。

拒否語（上の表）が1つでも入っていれば、そのqualityは**低い極（0.0）に着地**します。
語はブリーフ側の語彙をそのまま輸入しています — 「無しで」が曲を頼むときと直すときで
違う意味になってはいけないので、程度語と同じ扱いです。

```
- reading: ghost refused, down to the low pole
    (song-wide): bass.ghost_note_probability 0.34 -> 0.0
```

**`space` だけは拒否できません。**この質だけは語そのものが既に「少なくする」を意味して
いるので、「スペースは要らない」は密度を*上げる*依頼になります。この解析器は語の袋であって
位置を持たないため、それと「隙間を作っていたものを消す」を区別できません。**推測せずに
エラーにします**（「密度を上げて」と言い直してください）。

同じ理由で二重否定も読めません。「ゴーストノートを減らさないで」は減少として読まれます
（既知の誤りとしてテストに固定）。ブリーフ側が二重否定を数えられるのは、どの否定がどの
言及に付いているかを知っているからです。

### 語彙は自分自身をsweepします

ブリーフ側には保存済みsweepと2人目の読み手がいます（`sweeps/2026-08-19/`）。こちら側には
どちらも無いので、**語彙から指示空間を生成して、Spec Diffの形を検査**します
（`tests/test_edit_sweep.py`、約4000件を毎回）。

* 落ちない — `EditInstructionError` 以外の例外は1つも許さない
* 増加は上へ、減少は下へ、拒否は0.0へ。逆向きは無い
* 場所を指定したらそこだけ動く（動かせないときは `scope_warnings` に出る）
* パートを指定したらそのパートの密度だけ動く
* 全セクションが「触れた／触れていない」のどちらかに1回ずつ現れる
* どの計画も、計画した対象のSongSpecに適用できる

**2026-08-19にこちら側で見つかった2件は、どちらもこのリストの中にあります** — #98 は
クラッシュ（「密度を上げて」が planner の中で `KeyError`）、#99 は拒否が値を*上げて*いた件。
どちらもモデルは要らず、空間を1回全部通すだけで出ました。

コーパスは保存せず**語彙から生成**するので、明日 `QUALITY_WORDS` に語を足せば明日から
sweepされます。逆に、**誰も書いていない言い回しは生成できません** — 動詞と場所の形は
手書きなので、そこに無い形についてこのsweepは何も言いません。

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

**修飾された名詞句を拒否すると、拒否は修飾のほうにかかります。**
「暗すぎるシンセリードは避けて」はリードを**要求**したまま、暗さだけを拒否します
（＝暗すぎないシンセリードなら可）。2026-08-19に2つの読みを並べて決めた**解釈**であって、
測って出た事実ではありません — モデルは暗さだけを報告し、リードについては何も言いませんでした。

```
"暗すぎるシンセリードは避けて"   → dark を拒否、synth は要求のまま
"暗すぎるサイケなシンセは避けて" → dark と psychedelic を拒否（修飾は何段でも修飾）
"暗いシンセとスラップは避けて"   → dark と slap を拒否、synth は要求のまま
"シンセは避けて"                 → synth を拒否（修飾が無ければ従来どおり）
"no dark synth"                  → 同じ（英語も語順どおり、後ろが被修飾語）
```

**反対の読みは失われます。**「サイケなアルペジオは無しで」は「素のアルペジオなら可」に
なるので、アルペジオ自体が要らないときは修飾を付けずに「アルペジオは無しで」と書きます。

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

## 候補の順位付け（v0.2: 何が差を作ったかまで言う）

`report`の順位は`(blockingな欠陥, 整合度)`の2つだけで決まります。これは「どれが最高得点か」には
答えますが、**なぜ勝ったのか**も、**0.3点差と20点差の違い**も言えません。テイクが6本あり、
しかも指定した和音進行・キーが鳴らない（「v0.1の既知の限界」参照）以上、選ぶ側が唯一残ったレバーです。

```bash
python3 -m kihachi_music_ai shortlist projects/my-song \
  --also projects/my-song-rev01 \
  --also projects/my-song-rev02
```

規則は2つです。

**動かない次元は何も決められません。** review v0.1は`key`と`chords`に合計0.45のweightを
割いていましたが、`example_output`の25テイクを集計すると`key`は**全テイクで0.350の定数**、
`chords`は0.000〜0.098でした。v0.4で両方とも外しましたが、それは後から気づいたことです。
だから検査をコード側に入れました — 各次元を**その候補集合の中で**先に測り、ばらつきが
`spread_floor`（0.05）に届かない次元は「動かなかった」と表示したうえで、weightに関わらず
判断根拠から外します。生き残った次元でweightを再正規化するので、1次元しか動かない集合でも
得点が全員ゼロ近辺に潰れません。

**floorを下回る差は差ではありません。** 上位2本が`margin_floor`（3.0点）以内なら、
勝者を名指しせず`too_close_to_call`とし、`decide`コマンドの`--selected`も空欄のままにします
（直前の行で「決められない」と言っておきながら、実行可能な選択を手渡すのは矛盾です）。
**全次元が動かなかった場合も同じ扱い**です。それは「順位付け不能」ではなく最も強い同着で、
どちらにせよ耳で分けるしかないからです。

同着のときだけ、warning欠陥の少ないテイクを`tie_break`として添えます。**得点には入れません** —
warningは閾値付近の測定値であり、repaintはクリックを消さず移動させることがあるので、
点数を与えるには足りず、数字が尽きた後で触れるには足ります。

実際の3テイクに当てると、こうなります。

```text
  #1 100.00  aligned        discontinuity              ace-step-15-ssh-smoke-001
  #2 100.00  aligned        clean                      ace-step-15-ssh-smoke-001-clickfix01
  #3  66.67  aligned        discontinuity              ace-step-15-ssh-smoke-001-rev01
- decided on: section_boundaries (spread 0.333)
- identical across these takes, so not used: duration, midi:coverage, midi:harmony,
  midi:key, midi:section_energy, section_energy, tempo
- too close to call: ... are within 3.0 points; listen to all of them
- caution: this is a ranking on section_boundaries alone
- cleanest of the tied takes: ace-step-15-ssh-smoke-001-clickfix01
```

比較できるのは**同一SongSpecのテイクだけ**です（ADR-0005の規則を3本以上へ広げたものです）。
設計が違えば`different_song_spec`、未スキャンなら`not_scanned`、blocking欠陥があれば
`blocking_defect`として、順位に入れずに除外理由を表示します。

### stemバランスは報告しますが、採点はしません

`not_judged`に並べている項目のうち「ハーモニーがどれだけ聴こえるか」だけは、stem分離があるので測れます。
2026-08-16に、同一設計・seed違いの5テイクを`htdemucs`で分離して測りました。

| take | drums | bass | other | vocals | drums+bass | harmony |
|---|---|---|---|---|---|---|
| roll-101 | 60.5% | 35.3% | 4.1% | 0.1% | 95.8% | 4.2% |
| roll-102 | 62.7% | 28.1% | 9.2% | 0.0% | 90.8% | **9.2%** |
| roll-103 | 80.6% | 14.8% | 4.6% | 0.1% | 95.4% | 4.6% |
| roll-104 | 65.4% | 26.6% | 8.0% | 0.0% | 92.0% | 8.0% |
| roll-105 | 57.9% | 38.4% | 3.6% | 0.1% | 96.3% | **3.8%** |

**harmonyは3.8%から9.2%まで、2.4倍動きます。**drumsは57.9〜80.6%、bassは14.8〜38.4%で、
どちらも20ポイント以上動きます。設計は1つ、変えたのはseedだけです。
つまり判別力はあり、`key`の0.350のように定数で終わる次元ではありません。

**それでもスコアには入れません。向きが決められないからです。**
SongSpecはバランスの目標値を持たず、ハーモニーが大きいテイクが良いテイクだとは限りません。
実際この5本でも、harmony最大の`roll-102`は1位ですが、2番目に大きい`roll-104`は4位です — 順位と対応していません。

そこで`shortlist`は、順位の**横に**stemバランスを並べます。採点には触れません。
「この5本はハーモニーの聴こえ方が2.4倍違う。それがこの中で一番大きな音楽的差で、
どちらが欲しいかは聴く人にしか決められない」を伝えるためです。

`stems import`が`stem_manifest.json`へ各stemの`energy_share`を記録します
（manifestは`stem-manifest-v2`。v1のmanifestもそのまま読めます — shareが無いだけです）。
stemを取り込んでいないテイクは、順位からは外さず「未取り込み」として名前を出します。

### LLMにブリーフを読ませる（v0.2）

ADR-0011の第2段階です。**LLMは曲を書きません。**ブリーフを既存の語彙へ翻訳し、
翻訳できなかった語句を名指しします。

```bash
# 送信内容だけを書き出す（ネットワークもキーも不要）
python3 -m kihachi_music_ai intent prepare "アンビエント。きらびやかで高域中心、繊細。"

# 実際に読ませる（ANTHROPIC_API_KEY が要ります）
python3 -m kihachi_music_ai intent read "..." --output projects/my-song
```

ACE-Stepアダプター（ADR-0002）と同じ二段構えです。`prepare`はキー無しで送信内容を全部見せ、
`read`だけがネットワークに出ます。APIキーは環境変数からのみ読み、
**リクエスト記録にも成果物にもCLI出力にも残しません**。

### 検証は2つです

**1つ目はスキーマ。**返せるtrait名は`intent.TRAIT_WORDS`の25個だけで、polarityは±1、
strengthは0.5/1.0/1.5のいずれかです（脳が固定の閾値で読むので、0.73は比較のどちら側に
落ちるか分かりません）。

**2つ目はスキーマでは掛けられません** — 各traitの根拠と`unmapped`の各語句が、
**ブリーフに実在するか**の照合です。捏造された読みはこの形で現れます。
どちらか1つでも外れたら**文書ごと拒否**します（`import-kihachi`と同じ粒度で、
半端に適用された読みのほうが悪いためです）。

### 依存関係

`anthropic` SDKは**optional dependency**（`[llm]` extra）で、呼び出しの内側で
遅延importします。ADR-0001の「コアは標準ライブラリだけ」は動かしません。
自前HTTPで依存を避けるほうが筋が悪いと判断しています — 公式SDKが支援される
クライアントであり、守るべき境界は「**コアが**stdlibであること」だからです。

**2026-08-17に実APIで実行しました。**`prepare`・スキーマ・検証に加えて、呼び出し自体も
確認済みです。残高不足・キー不正・モデル名誤りは1行のエラーになります（トレースバックでは
ありません）。

### 2つの読み手を突き合わせる（v0.2）

同じ語彙をルールベース（`intent.read`）とモデル（`intent read`）の2つが読んでいて、
**両者を比べる仕組みがありませんでした**。

```bash
python3 -m kihachi_music_ai compare-readings <project または intent_reading.json>
```

保存済みの`intent_reading.json`を読むだけなので、**キーもネットワークも再課金も不要**です
（ブリーフは成果物の中に入っており、`brief_sha256`で取り違えを弾きます）。

```text
  arp            strength_differs
      model: +1 from 'アルペジオ'
      rules: +1.5 from 'アルペジ'
```

**どちらが正しいとも判定しません。**差分を出して止まります。モデルを正解と置くと、
ルールの定義にネットワーク呼び出しが混ざるためです。polarity差（要求と拒否が逆）を
最も重い差分として先頭に並べます。

初回に既存の成果物2件へ当てて、**ルール側のバグが2件出ました** — `_strength`が節頭まで
遡っていたため`かなりサイケなアルペジオ`のアルペジオまで1.5になっていた件と、
`サブベースは少しだけ`のような後置の程度語が読めていなかった件です。どちらも
モデル側が正しく、2026-08-17に修正済みです。**この2件はテストが全部通っている状態で
存在していました** — 比べるまで、どちらの読み手も自分が正しいと言い続けます。

#### sweepはリポジトリの中にあります（v0.2）

`compare-readings`は**誰かが書いたブリーフの上でしか**動きません。それ以前のsweepは
すべてセッションのスクラッチに書かれて消えていたため、`intent.py`を触るたびに、既に
存在していたコーパスに再課金していました。30ブリーフとその日のモデルの読みを
`sweeps/2026-08-19/`に置いてあります。

```bash
python3 -m kihachi_music_ai compare-readings sweeps/2026-08-19/readings/s01
```

`tests/test_sweep.py`が30件すべてを回し、**既知の8件の不一致だけが残っていること**を
テストします。手で回す必要はなく、不一致が増えても減っても落ちます。8件それぞれが
なぜ開いたままなのかは`KNOWN`に1件ずつ書いてあります — s01の「暗すぎるシンセリードは
避けて」が`synth`まで拒否するのかという**意味の問題**、「め」の読みのばらつき2件、
モデル側が誤っている2件、`アルペジ`と`シーケンス`が同じtraitである語彙の限界、
そして#66と#86の既知2件です。

**閉じたら、そのエントリを同じコミットで消してください。**どちらの読み手が動いたのかは
コミットメッセージにしか書けません。

### 床の2つの値は、ノイズ対策ではありません

`spread_floor`（0.05）と`margin_floor`（3.0）が何を守っているのかを2026-08-16に測りました。
結論は**推定器のノイズではない**です。ノイズは実測で2桁小さいためです。

**聴いて分からない摂動を音声に加える。**ゲイン0.999（0.0087 dB）、1フレーム移動（48 kHzで21マイクロ秒）、
1 LSBのディザ（-90 dBFS）。3テイク×3摂動で成分がどれだけ動いたか：

| 成分 | 最大の絶対変化 | 動いた回数 |
|---|---|---|
| duration | 0.0000 | 0/9 |
| tempo | **0.0007** | 3/9 |
| section_boundaries | 0.0000 | 0/9 |
| section_energy | 0.0000 | 0/9 |

スコアのドリフトは**最大0.02点**でした。`margin_floor`はその約150倍です。

**境界検出の0.5 dBという定数を振る。**`section_boundaries`だけは任意定数に乗っているので、
保存済み31テイクの小節別dBFSから検出ループを再現し、閾値を0.40〜0.60まで掃きました
（解析v0.2の3本は現行の検出器を再現できないので除外、28本）。

**27/28で recall は一切動きません。**動いたのは`quality-s42-nolyrics`の1本だけで、
定数を5%上げると 0.333 → 0.000 に落ちます。

したがって床は「測定誤差からランキングを守るもの」ではありません。守るものがほとんどないからです。
床が表しているのは**どれだけの差なら動く価値があるかという判断**であり、実測値ではありません。
出力JSONの`spread_floor_meaning`にもそう書いてあります。

`section_boundaries`が1ステップだけ離れているときの警告も、この実測に合わせて表現を変えました。
「検出が1小節ずれれば消える」ではなく（それは27/28で起きません）、
**差の全部が境界1本の判定であって、マージンではない**、が正確な言い方です。

### 焼き直して選ぶときの2つの落とし穴

**seedはSongSpecの一部です。**`composer.py`はseedからMIDIを生成するので、`song_spec.json`のseedを
書き換えたテイクは**設計が違うテイク**であり、`different_song_spec`として除外されます。これは正しい挙動です。
同一設計のまま生成器だけ振り直すには、`prompt.json`のseedを変えて`--from-brief`で焼きます。
SongSpecは動かないのでハッシュが一致し、MIDIも同一のまま、変わるのはサンプリングだけです。

```bash
python3 -m kihachi_music_ai ace-step prepare projects/my-song-roll2 --from-brief prompt.json
python3 -m kihachi_music_ai ace-step render  projects/my-song-roll2 --from-brief prompt.json
```

**trimは全部にかけるか、1本もかけないかにしてください。**`duration`成分は設計尺との距離を測り、
2秒外れると0に張り付きます。`trim-tail`は無音を落とすぶんテイクを実際に短くするので、
trim済みのテイクは未trimのテイクに対して`duration`で不利になります。5本の焼き直しで
4本trim・1本未trimにしたとき、`duration`のspreadは**1.000**まで開き、順位はほぼそれで決まりました。
混在を検出したら`confounded`として警告します。

`--save`で`take_shortlist.json`に残ります。**採用はしません。** 出力には
`not_judged`（音色、歌唱、音楽的な面白さ、ハーモニーの当否）を必ず並べます。ここにある数字が
答えていない問いを、答えたように見せないためです。選ぶのは`decide`で、理由は聴いた人が書きます。

## レンダーを素材として切り出す（v0.2）

測定が一貫して示しているのは、**生成器が壊すのは時間をまたぐ設計だけ**だということです。
進行は完成ミックスで一致率0.0、tail guardはseed次第、整合スコアはseedだけで37→77。
一方でMIDIの進行はLive上で**56小節中56小節**一致します。

同じ失敗も、素材として使えば性質が変わります。

| 失敗 | 曲として | 素材として |
|---|---|---|
| 低域偏重 | 修正不能 | stem分離済みの素材として使える |
| tailが届かない | 毎回賭け | 端を使わなければ無関係 |
| seedで大きく振れる | 切り分け不能 | 単に採否の問題 |
| キー・進行が違う | 修正不能 | **移調では直りません**（下記） |

32小節・seed違い3本で実測しました（32小節は「tail guardが届かない」と記録しているケースです）。

| take | 曲として | 中央4小節（9:13）を切ると |
|---|---|---|
| roll-201 | **blocking**（末尾無音2.58s） | **clean** |
| roll-202 | **blocking** + discontinuity | warning（`silent_gap`） |
| roll-203 | **blocking**（末尾無音2.58s） | **clean** |

**3本とも曲としては使えず、2本は素材として無傷でした。**

### キーは測れないので、移調では救えません

同一設計・seed違いの5テイクから中央4小節を切り、キーを推定しました。

| 素材 | 確信度の範囲 | 前後半の相関（中央値） | 中心ドリフト（最大） |
|---|---|---|---|
| 完成ミックスから | 0.049〜0.148 | 0.751 | 1.07半音 |
| ベースstemから | 0.054〜0.219 | 0.672 | 2.14半音 |

**閾値0.25を10本すべてが下回りました。**設計は全部D# minorですが、推定はF# minor /
D# minor / G# major / C# majorとばらばらです。単音のベースstemなら出ると考えて測りましたが、
三和音前提の推定器なので改善しませんでした。

ピッチ内容自体は安定しています（相関0.75、ドリフト1半音程度）。**使えないのではなく、
何から何へ移調すべきかが決まらない**のです。

### 単音法なら根音は読めます

原因は音声ではなく推定器でした。三和音前提のクロマを単音のベースに当てていたためです。
YINの累積平均正規化差分（`pitch.py`）を**同じ音声**に当てると、こうなります。

| take | 推定root | agreement | 2位 |
|---|---|---|---|
| roll-101 | **D#** | 0.747 | D 0.09 |
| roll-102 | **D#** | 0.658 | G# 0.27 |
| roll-103 | **D#** | 0.444 | G# 0.26 |
| roll-104 | G# | 0.350 | **D# 0.31**（ほぼ同着） |
| roll-105 | G# | 0.551 | F# 0.18 |

設計はD# minorです。**5本中3本で設計の主音が首位**、1本はほぼ同着。同じ音声に対して
クロマは何とも一致しませんでした。G#はD# minorの4度なので、調外ではなく別の和音です。

合成音での検証: 55〜110 Hzで誤差10セント未満、**第2倍音が基音の5倍でもオクターブ誤りなし**
（プレーンな自己相関が失敗する条件です）、無音は正しくunvoiced。

**それでも自動移調の根拠にはなりません。**

- 可聴frame（-40 dBFS超）の検出率は**61%**（テイクごとに48〜93%）
- agreementは0.35〜0.75で、2本は0.5未満
- **rootは調ではありません。**長短も、D#とG#のどちらが主音かも、単音線からは決まりません

適用範囲はこうなります。

- **音程を持たない素材（ドラム・パーカッション・質感）** — そのまま使えます。stem比で
  ドラムが最大（57.9〜80.6%）で、モデルが最も安定して作る部分でもあります
- **音程を持つ素材** — 根音の候補は出せます。移調の判断材料にはなりますが、**自動採用の
  根拠にはなりません**（ADR-0005以来と同じ形で、測定は判断を助けても代行しません）

```bash
python3 -m kihachi_music_ai cut-sample projects/my-song --bars 9:13 --name groove-a
```

`audio/samples/groove-a.wav` を書き、`sample_manifest.json` に素性を残します。
小節は1始まり、終端は排他的です（`9:13`は9小節目から4小節）。

### 短く焼かず、中央を切り出してください

2小節を要求することは、**生成器が最も苦手な領域へまっすぐ入る**ことです。32小節でも自分の
グリッドに2.32秒届かず、56小節でもseed 5本中4本で足りませんでした。不足量はほぼ一定なので、
短いほど割合が大きくなります。

16〜32小節を焼いて中央から切り出せば、冒頭の立ち上がりとguardが届かない末尾の両方を避けられます。

### 端の処理

各端を最寄りのゼロ交差へスナップします。**交差ペアのうちゼロに近い方**を選びます —
交差した直後のフレームは、8 kHzの220 Hz音で見るとピークの17%あり、避けたかったクリックと
同じ大きさになるためです。上限は10 ms（110 BPMで1小節の0.5%）で、届かない端はフェードし、
その旨を`edges`に記録します。

実レンダーから4小節切り出した実測では、ループ点の跳躍は**ピークの0.056%**（-65 dB相当）でした。

### 中央を切っても、素材のクリックは避けられません

避けられるのは「モデルが苦手な端」だけです。**素材が真ん中に持っているものは付いてきます。**

実際に`--bars 27:31`で切ったサンプルは、**このレンダーの最大の跳躍（0.5884）をちょうど囲って**
いました（レンダー61.55秒 ＝ 27小節目の開始56.73秒 + 4.823秒）。既知の欠陥位置は
`material_defects.json`にあるので、`cut-sample`は窓がそれを含むと警告します。

```text
- carries a known warning discontinuity at 4.823 s into the sample
  (61.550 s in the render); the cut did not make it, but the window kept it
```

**警告が出ないことは「綺麗」を意味しません。**レンダー側の検査は**コードごとに1点、最悪の位置
だけ**を記録します。実際`--bars 9:13`のサンプルは警告なしですが、自分で検査すると7.107秒に
跳躍0.5507を持っています — レンダー最大ではないので位置が残っていないためです。
**切り出した後、サンプル自体を`scan_material`で測ってください。**

**キーとBPMは「指定した値」として記録します。**測定値ではありません。生成器がキーに従わない
ことは測定済みなので、素材のメタデータがそれを保証しているように書くことはできません。

詳細と、この設計を選んだ理由はADR-0010にあります。

### 素材の選別（`shortlist`の4次元は使えません）

duration・tempo・section_boundaries・section_energyはいずれもアレンジの整合を測るもので、
4小節のループでは全部死んでいます — 尺は切った通り、テンポと小節グリッドはSongSpec由来、
セクション境界は存在しません。そこで測り直しました。実際に切り出した13本での実測です。

```bash
python3 -m kihachi_music_ai review-samples projects/my-song --also projects/other-song
```

| 次元 | 実測の幅 | 扱い |
|---|---|---|
| **on_grid_fraction** | **0.095〜0.935** | **採点する** |
| onsets_per_bar | 1.25〜12.25 | 報告のみ |
| low_to_high | 8.5〜148.9 | 報告のみ |
| rms_dbfs | -23.2〜-16.5 | 報告のみ |

**グリッド整合だけは採点します。好みの問題ではないからです。**小節グリッド上で切ったのに
トランジェントがそのグリッドに乗らない素材は、**自分が持つメタデータと中身が食い違っています**。
「疎か密か」とは違って、これは間違いです。

実例として、`roll-103`のサンプルは10.5打点/barと密なのに**9.5%しかグリッドに乗りません**。
`roll-104`は7.75打点/barで93.5%です。前者はループとして使えず、後者はそのまま置けます。

密度・レベル・明るさは**報告のみ**です。疎なループが密なループより悪いわけではないので、
向きが決められません（[stemバランス](#stemバランスは報告しますが採点はしません)と同じ理由です）。

**打点が少なすぎるときは`undetermined`と答えます。**持続音のベースstemは、4小節で9打点から
`on_grid_fraction` 1.000を返しました。それは「グリッドに乗っている」ではありません。
12打点を下回る素材は、順位付けの対象から外して「判定できない」と表示します
（悪いのではなく、この指標が語れないだけです）。

**単体stemから切った素材には、mix較正の指標を当てません。**`low_to_high`はstemで
720〜612,993まで発散し、ベースstemは本来モノラルに近いので`mono_collapse`が出ます。
どちらも測定値としては正しく、素材の欠陥として読むのが誤りです。該当する所見には`*`を付けます。

## Audio-to-MIDI（v0.2: 素材を音符に戻す）

v0.1の境界はAudio-to-MIDIを次段階に挙げつつ、留保も付けていました — **レンダーを写しても、
取れるのはモデルが作ったものであって、KIHACHIの設計ではありません**。設計は最初からMIDIにあります。
ADR-0010でレンダーが曲でなくなったことで、この留保は解消しました。**写す価値があるのは素材**です。

```bash
python3 -m kihachi_music_ai transcribe-sample projects/my-song --name mid-bass
```

`audio/samples/mid-bass.mid` を書きます。BPMはmanifestから取ります（その拍で切ったので、
4小節から推定し直すのは既に分かっていることを当てに行くだけです）。

**音高はトラッカー、時刻はオンセットから取ります。**トラッカーのホップは128 ms＝120 BPMで
4分の1拍あり、それだけでは使い物になりません。合成した4音のラインで、トラッカー単独の開始は
最大0.23拍ずれ、**オンセット併用で0/1/2/3拍ちょうど**になりました。

### 単音だけです。フルミックスは0音を返します

実測です。同じプロジェクトのフルミックス4小節は **voiced 1%、0音**。分離したベースstemの
同じ区間は **5〜8音**でした。混合をトラッカーに渡すと、**でっち上げずに黙る**のが正しい挙動です。

したがって手順は `render → stems → cut-sample → transcribe-sample` になります。

### 出力は「どれだけ取れなかったか」を必ず持ちます

`voiced_fraction`は休符と取りこぼしの両方を含みます。可聴frameに対する検出率は
**61%**（5本のベースstemで48〜93%）なので、静かな素材や非調和的な素材は
**間違った音符ではなく穴として**返ってきます。音符の数が完全性を意味しないよう、
これを常に添えます。

### 実機で確認しました

転写した8音をLiveのMIDIクリップとして作り、Live側に独立に解析させました。

- `auto_inferred_role: "bass"` — **Liveが独立にベースラインと分類**
- `maximum_notes_at_same_onset: 1` — **単音であることが裏取りされました**
- 小節ごとの根音: **Eb → Ab → C# → Ab**

設計の進行は`D#m - B - F# - C#`なので、1小節目（Eb = D#）は一致し、以降は違います。
**これは失敗ではなく、この機能の限界そのものです** — 写しているのはモデルが弾いた音であって、
設計ではありません。進行が守られないことは既知（一致率0.0）で、その事実がそのまま転写に現れます。

## Live展開（v0.2: 実機で通しました）

`ableton-plan`が出す操作リストを、2026-08-16に**実際のLiveセットへ通しました**。
経路はKIHACHIがMCPツールを直接叩くのではなく、AbletonGPT側の検証器です。

```bash
python3 -m kihachi_music_ai ableton-plan projects/my-song --first-track-index 1
# AbletonGPT側（別リポジトリ）で検証 → 適用
python -m abletongpt.cli.jobs import-kihachi --arrangement-plan .../arrangement_plan.json --out job_plan.json
python -m abletongpt.cli.jobs run --plan job_plan.json
```

56小節・1389ノート・13操作が`completed=13 failed=0`で通り、3トラック
（Drums / Bass / Chords）にクリップが0〜224拍で並びました。ドラムには実在キット
（909 Core Kit）、ベースにはOperatorがロードされています。

**2026-08-17に、素材とオートメーションを含めた16操作を一度に通しました**
（`completed=16 failed=0`）。MIDI 3本が0〜224拍、切り出したサンプルが0〜16拍で
同じArrangementに並び、`set_clip_parameter_envelope`の4ステップも同じ実行に含まれます。
ノートはKIHACHIの計画ファイルからAbletonGPTが直接読むので、**1389音がどの対話にも
乗りません**。読み戻したchordsクリップは402音・根音56/56一致で、上と同じ結果です。

**2026-08-17、今日足した語彙だけで書いたブリーフをLiveまで通しました**
（`example_output/scoped-vocabulary-live-001`、seed 8、`completed=13 failed=0`）。

```
テクノ。110 BPM、Cm。32小節。歯切れよく、シンコペを効かせて。手弾きっぽく。
前半は淡々と、後半は手数を多く。展開が速い。
```

`read-brief`は10文中9文が読まれたと報告します（未読は`32小節`——小節数を読むreaderが
無いのは既知）。**測りたかったのは、これらの語がプロンプト文字列ではなくMIDIとして
残るかどうか**で、Liveから読み戻した数字がそれを言います:

| 読み戻した値 | 期待値 | 何が届いた証拠か |
|---|---|---|
| chordsクリップ 216音・平均duration **0.101拍** | 0.16 × 0.633 | `note_length`（歯切れよく）|
| bassクリップ 145音・平均duration **0.189拍** | 0.30 × 0.633 | 同上 |
| 根音 **32小節/32一致**（C,C,Bb,Bb…）| 1小節ごとに和音 | `harmonic_rhythm_bars=1`（展開が速い）|
| セクション別音数 130 / 166 / 224 / 233 | 前半が縮み後半が膨らむ | scope付き`flat`と`busy`（ADR-0013）|
| `drums.kick_density` 0.85 のまま | 曲全体のキットは動かない | scopeが曲全体へ漏れていない |

**範囲指定が実機で確認できたのはこの最後の行です。**「後半は手数を多く」が曲のキットを
上げていたら、前半も一緒に太っていました。セクション別の音数だけを見ても気づけません。

**2026-08-17、ブルースのシャッフルをLiveで確認しました**
（`example_output/blues-shuffle-live-001`、100 BPM、seed 8、`completed=13 failed=0`）。
読み戻しは`plan_quantize_midi_timing`（読み取り専用）を8分グリッドに当てる方法を使いました:
**304音中268音がグリッド外、最大ずれ0.1729拍**。三連の裏は1/6＝0.1667拍なので、
humanizeのジッタを足した値がそのまま出ています。

**そしてこれは、実機に通すまで間違っていた変更でもあります。**PR #59は`meter`の
`12/8`から`groove.swing = 0.667`を導きました。しかし`groove.swing`は**位置ではなく
傾き**で、composerのずれは`(swing - 0.5) × 0.35`拍です。つまり0.667では裏が0.558拍
——シャッフルの3分の1にしかならず、名前だけを見て0.667にしたのが原因でした。
`composer.swing_for_offbeat(2/3)`で換算すると0.9762で、これで裏が0.667拍に来ます。
**ローカルの測定は全部通っていました**（音符は動いていたので）。動いた量が音楽的に
足りないことは、鳴らして初めて分かりました。

**取り込んだ音声もArrangementへ運びます。**当初は`import_vocal_take`の後にコピーが無く、
素材がSessionスロットに置かれたままでした。MIDIは3本ともタイムラインに並ぶのに音声だけが
残るという非対称で、**実機に通して見るまで気づけませんでした**（テストは全部通っていて、
どれもそこを見ていませんでした）。`duplicate_clip_to_arrangement`はクリップ種別を問わないので、
欠けていたのは計画の側です。

**設計した進行がそのまま鳴っています。**Live上のchordsクリップを読み戻すと、
根音は全56小節で`Eb → B → F# → C#`（Eb = D#の異名同音）。SongSpecの
`D#m - B - F# - C#`と**56/56で一致**します。同じ設計の音声レンダーは
一致率0.0でした（「v0.1の既知の限界」参照）。**MIDI経路は設計を保持し、音声経路は失います。**

### 実機で見つかった不一致

計画は`apply_live_drum_kit`に`live_edition`を渡していました。**このツールは
その引数を取りません**（`apply_live_instrument_selection`は取ります）。AbletonGPTは
署名でパラメータを束ねるので、この操作は実行すれば失敗します。テストは
**間違ったほうのキー集合を固定していました**。実機に通すまで、両方とも気づけていません。

### オートメーションの値はパラメーターの単位です

`--automate`の`low`/`high`は、**そのパラメーター自身の単位**で書きます。正規化された0..1ではありません。

Remote Scriptは値をパラメーターの`min`/`max`で検証します。909 Core Kitの`Low Gain`は0〜127なので、
そこへ0.38を書くと**エラーにならず、範囲の0.3%が書かれます**。38%ではありません。
KIHACHIは以前`low`/`high`を0..1に強制していたので、範囲が0..1でないパラメーターでは
**静かに間違った値**を書いていたことになります。

`min`/`max`は`device_index`・`parameter_index`と同じ`get_track_devices`の出力にあります。

```bash
# Low Gain (0〜127) を 40〜90 の帯で動かす
--automate chords:fx_amount:0:1:40:90
```

**sendは例外で0..1のままです。**sendは本当に0..1だからです。

なお`set_clip_parameter_envelope`はSessionクリップ専用です（LiveのAPIはArrangementクリップに
対してnullを返します）。計画はenvelopeを書いてから`copy_session_clip_to_arrangement`する順序で
操作を並べます。

2026-08-16に実機で確認しました。`909 Core Kit`の`Low Gain`（`min 0 / max 127`、既定63.35＝0 dB）へ、
旧KIHACHIが送っていた値をそのまま書くとこうなります。

| 送った値 | 書き込み | 実際の位置 |
|---|---|---|
| 0.38 / 0.41 / 0.572 / 0.62 | **全て成功**（`matches: true`） | 範囲の0.3〜0.5%＝低域ほぼ全カット |
| 55 / 57.5 / 71 / 75（修正後） | 全て成功 | 範囲の43〜59%、セクションごとに上昇 |

**旧の値はエラーを返しません。**成功と報告したうえで、意図と正反対の位置に着地します。

### 取り込み契約は、planner の出力に追いつく必要があります

`ableton-plan`が出しうる操作は9つで、AbletonGPTの`import-kihachi`は現在その9つを受けます。

**2026-08-16まで、そのうち2つが受け付けられていませんでした** —
`set_clip_parameter_envelope`（`--automate`）と`import_vocal_take`
（`--reference-audio` / `--vocal-audio`）です。どちらもMCPツールとしては動いていて、
欠けていたのはジョブ経路だけでした（AbletonGPT0.2#137で追加）。

```text
operation 13 uses unsupported KIHACHI core command 'import_vocal_take'
(allowed: apply_live_drum_kit, ..., set_clip_send_envelope, set_tempo)
```

**取り込みは最初の未対応操作で文書ごと拒否します。**envelope1個のために計画全体が
1操作も適用されません。この挙動自体は正しく、危険な半端適用を防ぎます。問題は、
受け入れ集合がplannerの出力から遅れると、**計画を作り終えて実行系を探しに行くまで
気づけない**ことでした。

そこでKIHACHIは、契約外の操作を含む計画を作った時点で警告します。

```text
- warning: <op> is outside AbletonGPT's import-kihachi contract;
  importing this plan refuses the whole document. Run these operations through
  the MCP tools directly, or build the plan without them
```

現在この警告は出ません（9操作すべてが契約内です）。**次に操作を足したとき、
AbletonGPT側にハンドラが無ければ発火します** — 前の2つが見過ごされたのが、まさにその形でした。

`first_track_index`はセットの既存トラック数に一致させます。ずれていると
`apply_live_drum_kit`が既存トラックへキットを載せるので、AbletonGPTが適用直前に
`TrackBaselineMismatch`で止めます（KIHACHI側はLiveを見ないので、この検査は持てません）。

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
（coverage 0.23）。

### 低域偏重はKIHACHIの指定ではなく、モデルの癖です

stemのエネルギー比を測ると、**ドラムとベースで9割**を占め、ハーモニーは5〜10%しかありません。
これが指定の結果なのかを確かめるため、2026-08-15に**指定を反転させた対照テイク**を焼きました。

| プロンプトの要求 | drums | bass | harmony | drums+bass | low/high | 6kHz超 |
|---|---|---|---|---|---|---|
| dominant bass / relentless hats | 61.3% | 33.8% | 4.8% | **95.1%** | 53.85 | 0.98% |
| dominant bass（32小節） | 23.8% | 65.8% | 10.4% | **89.6%** | 21.90 | 1.91% |
| supporting bass / sparse hats | 65.1% | 29.7% | 5.1% | **94.8%** | 123.98 | 0.36% |
| ＋低域を示す語を全排除 | 60.6% | 32.0% | 7.4% | **92.6%** | 195.26 | 0.18% |

`dominant and up-front slap electric bass`を`supporting fingered electric bass`へ、
`relentless 16th hats`を`sparse hats`へ反転させても、**ドラム+ベースは9割から動きません**。
4本目はProduction行から`deep sub control`と`club-ready dynamics`まで消し、
`airy open space / bright open top end / very light low end`に置き換えたものです
（`prompt.json`を手編集し`--from-brief`で送信）。

**それが最も暗いテイクになりました。**low/high 195.26、6 kHz以上は0.18%。
要求と結果が逆を向いています。

したがって低域偏重は**プロンプトでは制御できません**。`dull_high_end`が発火しても、
プロンプトの書き方を疑う必要はありません。

なお**単体stemにこの指標を当てはめてはいけません**。bass stemは高域がほぼゼロなので
low/high比が142089や919623まで発散します。閾値はミックスで較正されたものです。


## v0.1の既知の限界

**いずれも実測で確認した仕様上の限界であり、不具合ではありません。**「直そうとして時間を使わないため」に集めています。
詳細は各節にあります。

| 限界 | 実測 | 回避策 |
|---|---|---|
| コード進行がミックスから測れない | 29テイク中18テイクで一致率0.0、残りも最大0.167 | `midi-review`で照合する（MIDIは設計から書いているので推定不要）。**検出器のバグではありません** — 合成した進行を通すと一致率1.0 |
| 構造タグ`[inst]`と散文の`no vocal`が無視される | 2経路で指示して両方とも不通 | セクションを歌詞なしで`repaint`する。対象は`instrumental-plan`が出す |
| 指定した和音進行・キーが鳴らない | stem上で検出器が自信を持っても一致率0.0（2テイク）。request・プロンプト双方に正しく載っている | 現状なし。`midi-review`でKIHACHI側の設計が正しいことは確かめられる |
| turboが`inference_steps`を無視する | steps 8と60で音声がバイト単位で同一 | `seed`は効くので、変化が欲しければseedを変える |
| tail guardが届くかはseedごとに変わる | 32小節ではguard 2小節で2.32s不足（blocking）、4小節でも1.31s。56小節でも**同一設計5本中4本がblocking**（不足2.14〜3.82s、2026-08-16） | `--tail-guard-bars`を上げる、または`trim-tail`で後から落とす。**尺で足りる／足りないを決め打ちしない** — 毎テイク測る |
| repaintがクリックを消さず移動させることがある | 17秒テイクでは消失、2分テイクでは61.55s→63.73sへ移動（いずれもマスク内） | 測定値だけで追加レンダーを決めない。閾値付近（0.5前後）は可聴か先に聴く |
| 低域に寄る | ドラム+ベースで9割、ハーモニーは5〜10%。**指定を反転しても変わらない**（2026-08-15の対照テイク4本） | プロンプトでは制御できない。`dull_high_end`が出てもプロンプトを疑わない。単体stemには当てはめない（比が発散する） |
| `analyze_clip_warp_alignment`は切り出した素材には使えない | 4小節の`groove-a`をLiveへ取り込むと、warp marker 3個、`marker_alignment_ratio` 0.33、`onset_coverage_ratio` 0.013。**崩れているように読めるが、崩れていない**（2026-08-17） | クリップの拍数で判定する。小節ぴったりに切れていればLiveはマーカーを打たず線形に伸ばすだけで、この指標はフルテイク用。`groove-a`は110 BPMで16拍ちょうどに着地した |
| `intent`の語彙は比喩で書かれたブリーフをほとんど捕まえない | 「夜の高速道路を走っているような、暗くて疾走感のある」で当初trait 0件・全文`unmapped`。`dark`/`bright`追加後は`暗くて`のみ拾い、残る3節は依然`unmapped`（2026-08-17） | モデルの失敗ではなく仕様（ADR-0011）。`unmapped`を読んで語彙側を広げる。`疾走感`は2026-08-17に**構成要素へ分解して**届くようになった（密度＝`busy`、和音の変化速度＝`fast_changes`、音価＝`note_length`／ADR-0012）が、**その語自体は依然として`unmapped`**。分解して書けば届き、比喩のまま書けば届かない。残る入口は`視界が開ける`のようなセクション間の対比 |
| ジャンルはスウィングをほとんど教えてくれない | `groove.swing`を設定するジャンルは1021中`mutation_funk`（0.54）**だけ**。Jazzを含む23ファミリー全部が`None`＝0.5（ストレート）。「ジャズ」とだけ書いた曲はストレート8分で書かれます（2026-08-17） | ブリーフで明示する（`スウィング`／`シャッフル`／`跳ねる`）。2026-08-17以降、これらは`groove.swing`に届き、composerの裏拍が実際に動きます（0.607で110 BPM時20.4 ms）。**2026-08-17、データベース自身が言っている分だけ配線しました**：`meter`が`4/4; 12/8`と書く28ジャンル（全部Blues family）は`groove.swing`が**0.9762**になります（`groove.swing`は**位置ではなく傾き**で、オフビートの遅れは`(swing - 0.5) × 0.35`拍。0.667だとオフビートは0.558拍でシャッフルの3分の1にしかなりません——名前だけで0.667にして、実際に鳴らすまで気づきませんでした）。`6/8`の81ジャンルは読みません（6拍子は「スウィングした4拍子」ではなく別の拍子）。**Jazzは依然としてストレートです**——Jazz全41行のmeterは`4/4; 3/4; odd meters possible`で、`swing`という名前のジャンル自身を含めてスウィングを一言も言っていないからです。moodタグにも`swing`はありません（84種を全数確認）。ジャズを跳ねさせたければブリーフで言ってください |
| ハイフン入りのジャンル名は綴りを変えると見つからない（見つかっても別物） | 1020行中100行が名前にハイフンを持ちます（`Boogie-Woogie`、`Post-Rock`、`Jazz-Funk`）。検索されるのはハイフン綴りだけで、「boogie woogie」は**`Boogie`にマッチしていました**——別ファミリー、`4/4`（`Boogie-Woogie`は`4/4; 12/8`）。**間違ったジャンルは、見つからないより悪い**（2026-08-17） | ハイフンをスペースに置換した形と除去した形を表層形に追加（210形が増加）。実在する名前・別名は`setdefault`で常に優先されるので、`boogie`は`boogie`のままです |
| 日本語で名指せるジャンルは1020中131だけ | 別名を持つ行が131（うちsubgenreは983中103）。「シカゴブルース」「ブギウギ」は**何にもマッチしません**（SongSpecの既定ジャンルに落ちるので、黙って別の曲になります）（2026-08-17） | 英語名で書けば引けます（`chicago blues`）。データ側の穴なので、埋めるなら`genres.json`に別名を足す作業で、KIHACHI側の変更ではありません |
| ジャンルはシンコペーションを教えてくれない | `derive.Profile`にsyncopationの欄が無く、1021ジャンル全部が定数0.58のまま。動かせたのは`slap` traitだけでした。`edit.py`はv0.1から「シンコペ」の語を持っていたので、**出来上がった曲には言えて、作る前のブリーフには言えなかった**（2026-08-17） | ブリーフで明示する（`シンコペ`／`裏打ち`／`うねる`、逆は`オンビート`／`表打ち`）。`groove.syncopation`と`bass.syncopation`の両方に届き、8小節テクノ（seed 8）で「シンコペを効かせた」はベースを128→146音、drumsとsynthの配置も動かします。ジャンル表そのものは未修正 |
| ジャンルの人間っぽさは上書きできなかった | `groove.humanize`は23ファミリー**全部**が値を持つ（Hardcore Electronic 0.04〜Jazz 0.45）のに、ブリーフからは一言も届きませんでした。composerのjitterに直結する値で、`groove.py`いわく既定の0.18は110 BPMで±1.7 ms（2026-08-17） | ブリーフで明示する（`手弾き`／`ヨレ`／`人間っぽい`、逆は`タイト`／`かっちり`／`ジャスト`）。ジャンルの値を起点に動きます（「手弾きっぽいテクノ」で0.06→0.487、全パートのstartが最大4.0 msずれ、音程は不変）。極は0.7と0.02で、0.0にはしません（それはクオンタイズであって好みではない） |
| ドラムの手数はブリーフから動かせなかった | `drums.kick_density`／`hat_density`は音符の数そのものを決めるのに、届く語がありませんでした。`minimal`は近い語ですが別物で、冒頭2セクションの`minimal`フラグを立てるだけで密度には触れません（2026-08-17） | ブリーフで明示する（`手数`／`ぎっしり`／`詰め込`、逆は`スカスカ`／`余白`／`隙間`）。**上方向は天井で効かなくなります**：テクノは既に0.85/0.92で、0.95へ上げても8小節のdrumsは381音のまま変わりません（`build_pattern`の最大ステップに当たっているため）。ダブは0.38→0.76で240→272音、ヒップホップは328→365音。下方向はテクノでも効きます（381→320音） |
| コードの変わる速さはブリーフから動かせなかった | `harmony.harmonic_rhythm_bars`は全composer（bass/chords/arp/synth/sub）が和音を選ぶのに使い、`analyzer`と`midi_review`が答え合わせにも使う値です。23ファミリー全部が1・2・4のどれかを持つのに、届く語がありませんでした（2026-08-17） | ブリーフで明示する（`展開が速い`／`目まぐるしく変わる`、逆は`ワンコード`／`コードを引っ張って`）。**整数の3段ラダー（1/2/4）を1段ずつ動かします**：`少し`と無印はどちらも1段、`かなり`だけが端まで行きます（段の間に着地する値が無いため）。テクノ8小節で2→1にすると、bass/chords/synth/arp/vocoderの音程が半分入れ替わり、subは16→32音。ドラムは1音も動きません |
| 音の長さを表すフィールドが無かった | 各パートのdurationは`composer`に書かれた定数でした（bass 0.3、kick 0.16、synth 0.18）。ここまでの13語は「消費者はいるのに届かない数値」でしたが、これは**数値そのものが無かった**唯一の例です（2026-08-17） | `groove.note_length`を新設（1.0＝従来の定数そのもの、範囲0.25〜2.0）。ブリーフで`歯切れ`／`スタッカート`／`短く切って`、逆は`レガート`／`繋げて`／`伸ばして`。伸ばす方向は**同じパートの次の音まで**で頭打ちになります（それがlegatoの定義で、和音が次の和音へ食い込まない理由）。1.0のときはJSONに書き出しません（既存specのダイジェストが変わるため） |
| セクション間の対比はブリーフから指定できなかった | 各セクションのenergyと3つのdensityは`arrangement.py`のarchetypeが決めており、「メリハリのある」と「淡々とした」は**同じ4セクション**を作っていました（2026-08-17） | ブリーフで明示する（`メリハリ`／`起伏`／`抑揚`、逆は`淡々`／`平坦`／`一定`）。**平均のまわりで広げる／縮める**変換で、新しいフィールドは作りません（archetypeが選んだ形は曲のもので、これは「どこまで振り切るか」だけを言う）。テクノ32小節でセクションごとの音数は130/167/184/196 → メリハリで114/167/184/223（差66→109）、かなり淡々で178/175/175/174（差4）。**`かなりメリハリ`は`メリハリ`と同じ差**で頭打ちになります（0と1で切れるため） |
| ブリーフは「どこで」を言えなかった | `edit.py`はv0.1から`後半`／`序盤`／セクション名を解決できるのに、ブリーフ側には位置を表す語が1つもありませんでした（＝レンダー後には言えて、作る前には言えない）（2026-08-17） | `Trait.scope`を追加（ADR-0013）。`後半`／`前半`／`序盤`／`終盤`が効きます。**scopeが付くのは`busy`／`sparse`／`contrast`／`flat`の4語だけ**——セクションごとに存在する値がenergyと3つのdensityしかないためです。それ以外（例:「後半は暗く」）は**曲全体に効き、`brief`のカバレッジが`後半`を未読として報告します**。テクノ32小節で「前半は淡々と、後半は手数を多く」→ 130/167/184/196 が 130/159/224/223。「ここで視界が開ける」は依然として読めません（`ここ`は語ではなく文脈が指す場所） |
| 2人の読み手が場所について食い違っても見えなかった | scopeを足した日、`compare-readings`は「6 traitすべて一致」と報告しました。実際にはモデルは`前半は`／`後半は`を`unmapped`（＝この語彙では言えない）に入れ、ルール側はそれをscopeとして**使っていました**。範囲語はどのtraitのevidenceでもないので、trait同士の比較では原理的に見えません（2026-08-17） | モデルのスキーマに`scope`を追加し、`compare-readings`に`scope_differs`を追加。範囲語は**節ごとに**、その節にscope付きtraitがある場合だけ「読まれた」と数えます。新スキーマで読み直すと6 trait一致・`unmapped`空、「後半は暗く、終盤はスカスカに」ではモデルが`dark`を無scope・`後半`をunmappedにし、ルール側と完全に一致します |
| 1つのtraitを2つの語で呼ぶと、片方だけ拒否できない | 「アルペジは要らないが、シーケンスっぽさは残して」は`アルペジ`も`シーケンス`も同じ`arp` traitなので、同じノブにoffとonを同時に頼んでいます。モデルも同じ壁に当たり、後半を`unmapped`に入れました（2026-08-19、`sweeps/2026-08-19/readings/s19`） | 別々のtraitで言い直す。語彙を割るには`TRAIT_WORDS`に新しいtraitと、それを読む側の欄が要ります |
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
- Audio-to-MIDI、Ableton Live展開、LLM接続は次段階です。ACE-StepのAudio-to-Audioは構造保持用の`cover`と範囲再生成用の`repaint`に対応しています。
- stem分離はv0.2で取り込み済みです（ADR-0008）。複数候補については`shortlist`が順位と根拠を出しますが、**採用は自動化していません**（ADR-0009）。測れない次元が残る以上、最後は試聴です。

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

**2分の曲でも、既定の2小節で足りるとは限りません。**上の56小節の行はseed 8の1テイクです。
2026-08-16に**同じ設計をseedだけ変えて5本**焼き直したところ、足りたのは1本だけでした。

| seed | 音楽終端 | グリッド(122.182s)までの不足 | `silent_gap` |
|---|---|---|---|
| 101 | 120.04s | 2.14s | **blocking** |
| 102 | 121.58s | 0.60s | warning |
| 103 | 118.82s | 3.36s | **blocking** |
| 104 | 118.36s | 3.82s | **blocking** |
| 105 | 118.38s | 3.80s | **blocking** |

要求尺・プロンプト・guard幅は5本とも同一で、**違うのはseedだけ**です（`ace_step_request.json`の差分は`seed`のみ）。
つまり必要なguard幅は曲尺だけでなく**seedごとに変わり**、既定で足りるかどうかは焼いてみるまで分かりません。
`analyze`と`review`は毎テイク走らせてください。1本で足りたことは次の1本の保証になりません。

32小節ではさらに足りません。またguardを2小節（+4.36s）足しても音楽終端は1.01sしか後ろに動きません（追加分の約23%）。モデルは終止をずらすのではなく、与えられたバッファ全体で構成を組み直すためです。この比率だと32小節でグリッドに届かせるには6〜7小節のguardが要る計算になり、そのぶんGPU時間も伸びます。既定を一律に上げていないのはこのためです。

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
