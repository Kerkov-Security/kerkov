"""Harness — autonomous loop driver for Kerkov Bot.

Phase 1: task_list mode against local/CTF targets only. Driver lives outside
KerkovCLI and spawns fresh subprocesses per iteration; the model writes state
via HarnessStateTool, the driver authoritatively re-evaluates predicates.
"""
