"""How a command reaches the ACE-Step server.

Its own module because `revise` needs a client as much as `ace-step render`
does, and the revision loop should not have to import the whole ACE command
surface to get one.
"""

from __future__ import annotations

import argparse
import os

from ..adapters.ace_step import AceStepClient, AceStepConfig, AceStepLoraStatus


def ace_client(args: argparse.Namespace) -> AceStepClient:
    return AceStepClient(
        AceStepConfig(
            base_url=args.base_url,
            api_key=os.environ.get(args.api_key_env),
            request_timeout=args.request_timeout,
        )
    )


def print_lora_status(status: AceStepLoraStatus) -> None:
    print("ACE-Step LoRA status:")
    print(f"- loaded: {status.lora_loaded}")
    print(f"- enabled: {status.use_lora}")
    print(f"- scale: {status.lora_scale}")
    if status.adapter_type is not None:
        print(f"- type: {status.adapter_type}")
    if status.active_adapter is not None:
        print(f"- active adapter: {status.active_adapter}")
