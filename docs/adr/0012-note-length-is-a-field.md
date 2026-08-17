# ADR-0012: 音の長さはフィールドにする。ジャンル表には書かない

**Status:** Accepted
**Date:** 2026-08-17

## Context

2026-08-17に語彙を7組（`dark`/`swung`/`syncopated`/`loose`/`busy`/`fast_changes`
とその対極）広げた。**そのすべてが既存フィールドだった**。共通の手順はこうである:

1. SongSpecの値を列挙し、consumerがいるものを残す
2. そのうちブリーフから届かないものを探す
3. 語を足し、`music_brain`で配線し、composeされた音符で測る

この手順が2026-08-17に尽きた。残っていた`疾走感`は2つに分かれる:

* **どれくらいの頻度で音が来るか** — `busy`/`sparse`（ドラム密度）と`swung`、
  そして`fast_changes`（和音の変化速度）が既に届く
* **1音がどれくらい伸びるか** — **数値そのものが存在しない**

各パートのdurationは`composer`に書かれた定数だった（bass 0.3、kick 0.16、
synth 0.18、chordsはarticulation表の`duration`/`minimal_duration`）。
「歯切れよく」と書いても設定する先が無い。

## Decision

`groove.note_length`を新設する。各パートが書いたdurationに掛かる倍率で、
**1.0が従来の定数そのもの**。範囲は0.25〜2.0。

**ジャンル表（`derive.Profile`）には入れない。** `genres.json`は名前・別名・BPM
範囲・拍子・moodタグ・地域しか持たず、音の長さを言っていない。moodタグから
articulationを導くのは、対応表を発明してデータと呼ぶことである（`derive`の
モジュールdocstringが同じ理由で密度とarticulationを手書き表にしている）。
23ファミリー分を手で書く根拠が今は無いので、**全ジャンルが1.0**から始める。
ブリーフだけが動かす、この一覧で唯一のフィールドになる。

**伸ばす方向は同じパートの次の音で頭打ちにする。** それがlegatoの定義であり、
和音が次の和音へ食い込まない理由でもある。`compose_bass`と`compose_sub`は既に
自分でmonophonicにトリムしているので、この上限はポリフォニックなパートに同じ
仕事をしているだけである。

**1.0はJSONに書き出さない。** `example_output`のpre-engine specはダイジェスト
までピン留めされており、既定値を書き出すと変わっていないspecが書き換わる。
`instruments`と`preferences_fingerprint`が既に同じ扱いになっている。

## Consequences

* 既存の曲は1音も変わらない。golden MIDIバイトもspecダイジェストもそのまま。
* 「歯切れのいいテクノ」で0.633。8小節でbass/chords/arpのdurationがそれぞれ
  0.299→0.189、0.160→0.101、0.200→0.127に縮む。音符の数・音程・発音位置は不変で、
  **長さだけ**が動く。
* 「かなりレガート」で1.6。arpは音が詰まっているので上限が効き、どの音も次の音を
  越えない。
* `prompt_compiler`は1.0以外のときだけ`short, clipped notes` /
  `long, connected notes`を足す。既定のプロンプト文字列は変わらない。

## What this does not cover

* **`疾走感`そのものではない。**これはその半分（音価）で、もう半分（音の来る頻度）
  は別の語が担当している。どちらの語も全体を名乗らない。
* **ジャンルごとの既定値。**上に書いた通り、根拠が無いので書いていない。ファミリー
  ごとのarticulationを人が決める気になったとき、`derive.Profile`に欄を足すのが
  その置き場所である。
* **パートごとの指定。**「ベースだけ歯切れよく」は言えない。`note_length`は曲全体に
  掛かる。パート別にするなら`edit`の対象を増やす方が筋が良く、それは別の変更である。
