# ADR-0003: 生成Audio解析の境界

**Status:** Superseded by ADR-0004  
**Date:** 2026-08-09

## Context

ACE-Stepの生成成功だけでは、保存された音声の実尺、形式、level、無音、clip、tempoがSongSpecと一致するか判断できない。一方、v0.1で精度未検証のキー・コード推定まで導入すると、推定結果を事実として扱う危険がある。

## Decision

標準ライブラリだけでPCM WAVを読み取り、次を`audio_analysis.json`へ保存する。

- SHA-256とPCM形式情報
- peak、RMS、crest factor、DC offset、channel別RMS
- clipping sample率と-50 dBFS未満の20ms window率
- 20ms RMS positive fluxのautocorrelationによる推定BPMと信頼度
- SongSpecの目標尺・BPMとの差

解析はWAVを変更しない。キーはv0.1では観測せず、target keyと`not_analyzed_in_v0.1`を明記する。既存解析JSONは明示的な上書き指定なしで置換しない。

## Consequences

- ACE-Step、LoRA、AbletonGPTに依存せず生成物を機械的にQAできる。
- BPMは軽量推定であり、複雑なpolyrhythmやambient素材では低信頼になる可能性があるため、confidenceを必ず併記する。
- key、chord、section、stem、Audio-to-MIDI解析は別モジュールとして段階的に追加する。
