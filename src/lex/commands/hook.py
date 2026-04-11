from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    hook_parser = subparsers.add_parser("hook")
    hook_sub = hook_parser.add_subparsers(dest="hook_command", required=True)

    claude_parser = hook_sub.add_parser("claude")
    claude_sub = claude_parser.add_subparsers(dest="claude_hook_command", required=True)

    claude_stop = claude_sub.add_parser("stop")
    claude_stop.set_defaults(func=ctx.cmd_hook_claude_stop)

    claude_upr = claude_sub.add_parser("user-prompt-submit")
    claude_upr.set_defaults(func=ctx.cmd_hook_claude_user_prompt_submit)

    claude_ptu = claude_sub.add_parser("post-tool-use")
    claude_ptu.set_defaults(func=ctx.cmd_hook_claude_post_tool_use)
