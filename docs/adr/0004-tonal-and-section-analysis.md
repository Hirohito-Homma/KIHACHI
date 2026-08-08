# ADR-0004: キー・コード・セクション/エネルギー解析

**Status:** Accepted  
**Date:** 2026-08-09

## Context

生成AudioをMIDIへ変換する前に、ACE-StepがSongSpecの調性、コード進行、構成とエネルギーarcをどの程度反映したか、同じ小節grid上で比較する必要がある。完成mixにはdrums、vocal、delay、reverbが混在するため、推定labelだけを確定的な音楽情報として扱うことはできない。

## Decision

Audio Analyzer v0.2を標準ライブラリだけで実装し、次を`audio_analysis.json`へ追加する。

- mono化したPCMをbox averageで約4 kHzへdownsampleする。
- MIDI note 36–83の48音へGoertzel解析を行い、12 pitch classのchromaを作る。
- Krumhansl major/minor profileと、最初の信頼可能なコードによる弱いtonic anchorからキー候補を求める。profile scoreだけでなくbar chordのtonal confidenceも合成し、confidence 0.25未満は低信頼とする。
- SongSpecの小節長でmajor/minor triadを推定する。confidence 0.15未満のbarはlabelを参考値として残すが、コード進行一致率の分母には含めずcoverageを併記する。
- 20 ms RMS envelopeを小節ごとに集約し、10–90 percentileで正規化したenergy mapを作る。
- 境界前後2小節の平均energy差からsection候補を検出し、SongSpecの予定境界との1小節以内のrecallを保存する。
- SongSpecのsection energy列と観測energy列の相関を保存する。

解析はWAVとMIDIを変更しない。既存の解析JSONは明示的な`--overwrite`なしでは置換しない。

## Consequences

- Base生成とLoRA生成を同じSongSpec gridで比較できる。
- key、chord、sectionは推定値であり、confidence、coverage、境界距離を伴う検査結果として扱う。
- 相対長短調、enharmonic表記、非triad、転調、強い打楽器や空間系effectでは誤判定し得る。
- stem分離、専門DSP library、Audio-to-MIDI reconstructionはこの段階には含めない。
