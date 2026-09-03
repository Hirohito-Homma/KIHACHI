# ADR-0015: 人間承認付き Live 修復計画

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** KIHACHI Music AI project

## Context

VS7 は `ableton_verification.json` に Live 事後条件の不一致を残す。次に必要なのは「何を直すか」だが、AbletonGPT 側に失敗項目だけを安全に再適用する公開契約は確認できておらず、既存 Live Set のトラック・クリップを上書きしてよい許可もない。検証失敗を自動修復へ直結すると、重複トラックやクリップ上書きの期待損失が大きい。

## Decision

VS8 は Live を変更しない。`ableton_verification.json` を入力に `ableton_repair_plan.json` を出力する純粋な計画 Slice とする。

- Live 接続・Live 変更・AbletonGPT 実行・自動再検証・自動採用は行わない
- `status == fail` かつ元の arrangement operation を構造キーで一意に特定できるものだけ `candidate_reapply`
- track の index/name/count、`not_observable`、未知の category/status、0件または複数の対応はすべて `manual_inspection`
- `candidate_reapply` は「今すぐ安全に実行可能」ではなく「元操作に一意対応した」という意味だけである
- 全 source SHA、adopted round、再構築した expected state、書き込み直前 fingerprint が一致するまで plan を書かない

実際の再適用は、AbletonGPT 側の idempotency と既存 clip/track への扱いを確認したあとの別 Slice に残す。

## Options Considered

### A. 検証失敗から自動修復・自動再検証

| Dimension | Assessment |
|---|---|
| ユーザー価値 | 高い（Live が直る） |
| 事故時の損失 | 最大（重複・上書き） |
| 既存境界 | VS7 の読み取り専用契約を破る |

### B. 人間承認用の修復計画を生成（採用）

| Dimension | Assessment |
|---|---|
| ユーザー価値 | 不一致を次の判断へ接続する |
| 事故時の損失 | 小さい（Live 副作用なし） |
| 既存境界 | VS5–VS7 の provenance 検査を再利用できる |

### C. `verified` を最終納品 receipt へ昇格

| Dimension | Assessment |
|---|---|
| ユーザー価値 | 失敗時の前進がない |
| 既存境界 | 証拠の言い換えに留まる |
