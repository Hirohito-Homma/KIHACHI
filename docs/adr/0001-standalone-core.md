# ADR-0001: SongSpec中心の独立コア

**Status:** Accepted  
**Date:** 2026-08-08  
**Deciders:** KIHACHI Music AI project

## Context

v0.1は、自然言語からBass/Drums/Chords MIDIと音声生成プロンプトを作る必要がある。一方、既存AbletonGPTを壊さず、Live接続やACE-Step APIの有無に左右されない最小動作版にする。

## Decision

`Music Brain → SongSpec → Composer/Compiler` を独立Pythonパッケージとして実装する。SongSpecをモジュール間の唯一の曲設計契約とし、MIDI書き出しを含むコアは標準ライブラリだけで動かす。AbletonGPTと音声生成APIは将来のアダプター層に置く。

## Options Considered

### A. 独立コア（採用）

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Existing-system risk | Low |
| Testability | High |
| Future integration | Adapter required |

### B. AbletonGPTを直接import

| Dimension | Assessment |
|---|---|
| Complexity | Initially low |
| Existing-system risk | Medium |
| Testability | Runtime dependencies increase |
| Future integration | Tight coupling |

### C. 最初から共通ライブラリを抽出

| Dimension | Assessment |
|---|---|
| Complexity | High for v0.1 |
| Existing-system risk | Medium to high |
| Testability | High after migration |
| Future integration | High |

## Consequences

- AbletonGPTとLiveを起動せず、生成パイプラインをテストできる。
- SongSpecのバージョン管理が必要になる。
- 将来のLive展開では、SongSpecまたはMIDIノート計画をAbletonGPT形式へ変換する薄いアダプターを追加する。

