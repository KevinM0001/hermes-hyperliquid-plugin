"""hyperliquid-desk: a live Hyperliquid market dashboard for Hermes, on public data only.

register(ctx) only registers tools, the /hl command and the commentary
skill. It opens no socket, starts no thread and writes no file: the live
collector starts only when the user runs /hl start or presses Start in the
dashboard tab.
"""

from __future__ import annotations

from pathlib import Path

from .hl_desk import commands, runtime, tools

TOOLSET = "hyperliquid-desk"


def _tool_handler(name: str):
    def handler(args: dict, **kwargs) -> str:
        return tools.run_tool(name, args, runtime.get_runtime())
    handler.__name__ = f"{name}_handler"
    return handler


def register(ctx) -> None:
    runtime.set_settings_getter(lambda key, default=None: ctx.get_config(key, default=default))
    for name, schema in tools.SCHEMAS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=_tool_handler(name),
                          description=schema["description"])
    ctx.register_command("hl", handler=commands.handle_hl,
                         description="Hyperliquid desk: collector status, start, stop, restart")
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
