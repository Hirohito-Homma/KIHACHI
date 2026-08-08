# ADR-0005: 生成結果Reviewと修正提案の境界

**Status:** Accepted  
**Date:** 2026-08-09

## Context

Audio Analyzerが差分を測定しても、それだけでは次の生成で何を修正すべきか決まらない。一方、軽量推定を根拠にSongSpecやACE-Stepの状態を自動変更すると、誤判定をそのまま制作へ反映する危険がある。

## Decision

読み取り専用のReview層をAnalyzerとACE-Stepの間へ追加する。

- `song_spec.json`と`audio_analysis.json`から、尺、tempo、key、chord、section境界、section energyの整合componentを計算する。
- componentを固定weightで0–100へ集約し、`generation_review.json`へ保存する。
- scoreはSongSpecへの決定的な整合heuristicであり、音質や音楽的価値ではないことを常に記録する。
- evidenceとrecommendationを持つfindingから`revision_prompt.txt`を生成する。
- 任意のbaselineと比較するときは、完全に同一のSongSpecを持つprojectだけを許可する。
- ReviewはSongSpec、解析JSON、WAV、MIDI、ACE-Step model stateを変更しない。
- 既存のReview成果物は明示的な`--overwrite`なしでは置換しない。

## Consequences

- BaseとLoRAを同じ設計図に対して比較でき、次の生成指示を再現可能なartifactとして残せる。
- 低信頼のkey/chord推定はwarningとして明示し、確定的な観測として扱わない。
- scoreの上昇は音楽的に良くなったことを意味しない。試聴判断は別に必要である。
- `revision_prompt.txt`をACE-Stepへ自動投入する処理は、明示的な次段階として分離する。
