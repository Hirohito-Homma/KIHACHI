# ADR-0017: Guarded Device Repair Through AbletonGPT

**Status:** Accepted
**Date:** 2026-09-03
**Deciders:** KIHACHI Music AI project

## Context

VS9 は `set_tempo` だけを人間承認付きで実行する。VS8 の `device:<track-index>` candidate は元の `apply_live_instrument_selection` / `apply_live_drum_kit` に一意対応するが、それを replay すると既存デバイスの置換・キット再ロードになり得る。AbletonGPT PR #138 は同一プロセスの guarded primitive `repair_live_device` を追加した。許可される mutation は次のちょうど1つである:

- `set_device_parameter`
- `reset_device_parameter`
- `set_device_power`

契約は `get_track_devices` → identity/state 検証 → 0 または 1 mutation → 読み返し。insert / delete / replace / reorder はできない。

現行 VS7 の device 証拠はパラメータ値を持たない。`is_active`・名前・index・type は bounded snapshot に残る。パラメータ修復を証拠なしで組み立てると推測になる。

`candidate_reapply` は安全性の主張ではない。欠落デバイスを挿入で「直す」ことも、誤デバイスを置換することも、この primitive の目的に反する。

## Decision

VS10 は既存の `kihachi ableton-repair-apply` を拡張する。新しい CLI は作らない。

- tempo check は VS9 の JobPlan 経路のまま
- 一意の device candidate は、VS7 証拠が次を証明できるときだけ `set_device_power(enabled=True)` を1回送る:
  - 正しい track
  - 一意の device identity と index
  - デバイスは存在する
  - 観測時は inactive、期待は active
- `expected_track_name` / `expected_device_name` / `expected_power_state` を必ず付ける
- 欠落・誤 identity・誤 type・曖昧・パラメータ証拠不足は拒否し、挿入や JobPlan replay に落とさない
- Session clip / Arrangement / track は未実装のまま拒否
- `--prepare-only` は Live を読まず、JobPlan を偽造しない
- 実行は `--approve-plan-sha` の constant-time 比較が必須
- AbletonGPT の `repaired` / `noop` / `refused` / `failed` をそのまま記録し、retry しない
- 自動 `ableton-verify` は呼ばない
- KIHACHI プロセスは Live socket を開かない

## Consequences

- 安全に直せるのは「存在するが inactive な一意デバイス」に限られる
- 欠落デバイスは手動のまま残る（AbletonGPT も挿入できない）
- パラメータ修復は VS7 がパラメータ証拠を持つまで延期する
- tempo 経路は回帰テストで保存する

## Options Considered

### A. device candidate を元 JobPlan で replay

instrument / drum kit の再ロードは置換・無音キットになり得る。AbletonGPT #138 の目的に反する。

### B. 証拠不足でも `set_device_parameter` を組み立てる

VS7 に expected/observed パラメータが無い。推測になる。

### C. 証明できる power repair だけを guarded primitive へ委譲（採用）

実行範囲は狭いが、承認境界と AbletonGPT の stale-state ガードを壊さない。
