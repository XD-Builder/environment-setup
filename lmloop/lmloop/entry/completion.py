"""Zsh completion built from CommandMeta and the config key list.

The script is assembled from those registries. It is not a template with
placeholder surgery.
"""

from __future__ import annotations

import sys

from ..core.commands import (
    EVAL_FLAGS,
    GRAPH_PROPOSE_VERB,
    MCP_VERBS,
    MEMORY_ARG_CHOICES,
    cli_subcommand_metas,
)
from ..core.config import DEFAULTS
from ..ui import Console


def zsh_completion_script() -> str:
    """Self-contained zsh completion. Skill names refresh via ``lmloop skills names``."""
    subs = "\n".join(
        f"    '{c.name}:{c.desc.replace(chr(39), '')}'"
        for c in cli_subcommand_metas()
    )
    keys = " ".join(DEFAULTS)
    memory_verbs = " ".join(MEMORY_ARG_CHOICES)
    mcp_verbs = " ".join(MCP_VERBS)
    return (
        "#compdef lmloop\n"
        "\n"
        "_lmloop() {\n"
        "  local -a subs\n"
        "  subs=(\n"
        f"{subs}\n"
        "  )\n"
        "\n"
        "  local curcontext=\"$curcontext\" state\n"
        "  _arguments -C \\\n"
        "    '--model[override model for this run]:model:' \\\n"
        "    '--docker[run shell commands in an ephemeral container]' \\\n"
        "    '--docker-persist[reattach a long-lived sandbox container]' \\\n"
        "    '--docker-image[digest-pinned image override]:image:' \\\n"
        "    '--help[show help]' \\\n"
        "    '1: :->cmd' \\\n"
        "    '*:: :->args' && return\n"
        "\n"
        "  case $state in\n"
        "    cmd)\n"
        "      _describe -t commands 'lmloop command' subs\n"
        "      ;;\n"
        "    args)\n"
        "      case $words[1] in\n"
        "        skill)\n"
        "          if (( CURRENT == 2 )); then\n"
        "            local -a skills\n"
        "            skills=(${(f)\"$(lmloop skills names 2>/dev/null)\"})\n"
        "            _describe -t skills 'skill' skills\n"
        "          fi\n"
        "          ;;\n"
        "        skills)\n"
        "          if (( CURRENT == 2 )); then\n"
        "            compadd - names new\n"
        "          fi\n"
        "          ;;\n"
        "        config)\n"
        "          if (( CURRENT == 2 )); then\n"
        "            compadd - show get set\n"
        "          elif (( CURRENT == 3 )) && [[ $words[2] == get || $words[2] == set ]]; then\n"
        f"            compadd - {keys}\n"
        "          fi\n"
        "          ;;\n"
        "        completion)\n"
        "          # Only offer the supported shell name — avoid _values, which can\n"
        "          # surface unrelated zsh completion keywords in the menu.\n"
        "          (( CURRENT == 2 )) && compadd - zsh\n"
        "          ;;\n"
        "        memory)\n"
        "          if (( CURRENT == 2 )); then\n"
        f"            compadd - {memory_verbs}\n"
        "          fi\n"
        "          ;;\n"
        "        mcp)\n"
        "          if (( CURRENT == 2 )); then\n"
        f"            compadd - {mcp_verbs}\n"
        "          fi\n"
        "          ;;\n"
        "        retro|until|decisions|history|models)\n"
        "          ;;\n"
        "        graph)\n"
        f"          (( CURRENT == 2 )) && compadd - {GRAPH_PROPOSE_VERB}\n"
        "          ;;\n"
        "        eval)\n"
        f"          compadd - { ' '.join(EVAL_FLAGS) }\n"
        "          ;;\n"
        "        flow)\n"
        "          compadd - --json\n"
        "          ;;\n"
        "      esac\n"
        "      ;;\n"
        "  esac\n"
        "}\n"
        "\n"
        "compdef _lmloop lmloop\n"
    )


def cmd_completion(shell: str, console: Console) -> int:
    if shell != "zsh":
        console.error(f"unsupported shell '{shell}' (only zsh)")
        return 1
    sys.stdout.write(zsh_completion_script())
    return 0


def cmd_completion_cli(cfg, words: list, console: Console) -> int:
    shell = words[0] if words else ""
    if not shell:
        console.error("usage: lmloop completion zsh")
        return 1
    return cmd_completion(shell, console)
