# ADR-0002: ACE-Step 1.5 RESTアダプター

**Status:** Accepted  
**Date:** 2026-08-08

## Context

KIHACHI Music AIのSongSpecからACE-Stepで音声を生成したい。ただし、モデル本体、GPU環境、認証、サーバーの生存状態をコア作曲機能へ持ち込まない必要がある。

## Decision

公式ACE-Step 1.5の非同期REST APIを境界にする。アダプターは`release_task → query_result → v1/audio`を担当し、SongSpecやMIDI ComposerはACE-Stepをimportしない。

- 既定サーバーは`http://127.0.0.1:8001`
- APIキーは`ACESTEP_API_KEY`からのみ読み、成果物へ保存しない
- KIHACHIのBPM、キー、尺、seedを明示して送る
- 既定では`thinking`、入力format、CoT補完を無効化する
- 音声URLは設定したサーバーと同じoriginだけ許可する
- 既存のrequest/result/audioは明示的な`--overwrite`なしで置換しない
- LoRAは`/v1/lora/load → scale → toggle → status`でサーバーへ適用し、生成開始前にロード済み・有効・scale一致を検証する
- LoRAパスはACE-Stepサーバー側のパスとして扱い、ローカルファイルの存在確認や自動アップロードは行わない
- LoRA付き生成では、要求したパス・scale・adapter名と検証済み状態を結果JSONへ記録する

## Consequences

- ACE-Step未導入でもrequest JSONとテストを検証できる。
- 実音声生成には別途ACE-Step 1.5 RESTサーバーが必要。
- API仕様が変わった場合は、このアダプターだけを更新する。
- LoRAはサーバー全体のモデル状態を変更するため、共有サーバーでは`status`確認と運用上の排他制御が別途必要になる。
