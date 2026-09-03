# ADR-0016: 人間承認付き Live 修復実行（tempo 限定）

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** KIHACHI Music AI project

## Context

VS8 は `ableton_repair_plan.json` に `candidate_reapply` を残す。これは元 operation への一意対応であり、安全実行可能という意味ではない。AbletonGPT 0.2（main `5fcf063`）の現行契約では:

- `set_tempo` は値の設定であり、track/clip を複製しない
- instrument / drum kit は既存 device を読み、候補と違うなら置換せず拒否する
- `create_midi_clip` は空 slot へ作り、異なる既存 clip は上書きせず拒否する
- `copy_session_clip_to_arrangement` は copy 前の重複確認を行わない
- job runner の track baseline は full plan が track を append する前提であり、track-targeting だけの partial job にはそのまま適用できない

repair plan の `source_operation` は notes 等を縮約した表示用 view であり、実行入力ではない。Live Set の永続 ID は VS7 evidence に無く、選択 execution の公開契約も無い。実機 Live は CI で使えない。

全 candidate の一括実行、元 job の `--rerun` / `resume`、KIHACHI からの直接 Live 操作は、重複・上書きまたは責務境界破壊の損失が大きい。

## Decision

VS9 の supported repair operation は `set_tempo` のみとする。

- 人間承認は対象 check ID と `ableton_repair_plan.json` の完全 SHA-256 を CLI に明示する
- 実行入力は現行 `arrangement_plan.json` の full operation であり、repair plan の縮約 payload ではない
- execute 直前に AbletonGPT 経由で Live を読み取り専用 preflight し、verification 時の tempo と expected track 構造が変わっていないことを確認する
- すでに expected BPM なら job を実行せず `ableton-verify` を要求する
- KIHACHI は Live socket へ接続せず、AbletonGPT の external process を使う
- repair job plan と repair execution receipt は VS6 artifact と別名で保存する
- 自動 `ableton-verify`、自動 retry、自動採用、preference memory 追記は行わない
- 実行成功は「AbletonGPT job が exit 0」であり、「Live が修復済み」ではない

device / Session clip / Arrangement candidate は `unsupported_for_execution` として、外部 process や Live read の前に拒否する。

## Consequences

- 実行事故の範囲を `set_tempo` に限定できる
- 承認・provenance・receipt 契約を後続の guarded selective-run より先に確立できる
- VS8 の他 candidate は未解決のまま残る
- preflight と run は別 process であり、小さな TOCTOU が残る。完全な原子性は AbletonGPT 側の同一 process guarded API へ残す

## Options Considered

### A. 全 `candidate_reapply` を一括実行

candidate は安全性を証明しておらず、clip / Arrangement で上書き・重複し得る。

### B. 元の full job を `--rerun`

`create_track` が再実行され、track と clip を複製し得る。

### C. SHA 承認付き tempo 限定実行（採用）

修復範囲は狭いが、承認・receipt・readback 前提を一つの Vertical Slice で検証できる。
