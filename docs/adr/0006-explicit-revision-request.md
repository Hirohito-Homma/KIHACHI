# ADR-0006: ACE-Step改訂Requestの明示的適用

**Status:** Accepted  
**Date:** 2026-08-09

## Context

Review層が作る`revision_prompt.txt`を再生成へ反映する必要がある。ただしReview直後に自動送信すると、低信頼の解析結果を確認せず外部GPU処理と既存生成物の置換へ進む危険がある。

## Decision

- ACE-Stepの`prepare`と`render`は、明示的な`--revision-file`がある場合だけ修正指示を読む。
- 修正指示は通常prompt末尾の`Revision constraints`へ追加し、SongSpec由来のBPM、key、time signature、duration、seedを変更しない。
- 通常requestの`ace_step_request.json`は保持し、改訂版は`ace_step_revision_request.json`へ分離する。
- `prepare`は改訂版JSONを作るだけでネットワーク送信しない。
- 実際の`render`でも同じ明示指定を要求し、結果へrevision本文のSHA-256とrequestファイル名を記録する。
- 既存requestやrender成果物は、従来どおり明示的な`--overwrite`なしでは置換しない。

## Consequences

- Analyzer → Review → Revision requestの経路を追跡可能なartifactとして確認できる。
- 解析結果だけを根拠にGPU生成が自動開始されることはない。
- 改訂版生成を実行するか、どのLoRA scaleを使うかはユーザーの明示的な操作として残る。
