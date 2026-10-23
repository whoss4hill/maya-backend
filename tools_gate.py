"""Action gate for Maya's tools (not wired into /v1/chat yet; mocked-tested only).

Rules enforced here, in code, so a model reply can never skip them:
- every tool is OFF unless MAYA_TOOL_<NAME>=1 is set on the server;
- arguments are typed and checked against a per-tool spec; unknown tools/arguments are refused;
- tools marked `needs_confirm` do nothing until the owner has confirmed this exact call
  (same tool and same arguments) in a separate owner message; a changed argument needs a new confirmation;
- an explicit owner command that already contains every required argument (e.g. recipient and body)
  counts as the confirmation, a model-drafted argument does not;
- every decision is recorded through an audit callback.
"""
import json, os
from dataclasses import dataclass, field


@dataclass
class Tool:
    name: str
    args: dict            # arg name -> python type
    needs_confirm: bool = True
    run: callable = None


@dataclass
class Gate:
    tools: dict = field(default_factory=dict)
    audit: callable = lambda event: None

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    @staticmethod
    def enabled(name: str) -> bool:
        return os.environ.get(f"MAYA_TOOL_{name.upper()}", "0") == "1"

    @staticmethod
    def _key(name, args):
        return name + json.dumps(args, sort_keys=True)

    def call(self, name, args, owner_said=None, confirmed_key=None):
        """owner_said: dict of args the owner stated himself in his own command (may be partial).
        confirmed_key: key returned by an earlier 'needs_confirmation' result that the owner approved."""
        def done(status, **kw):
            self.audit({"tool": name, "status": status})
            return {"status": status, **kw}

        tool = self.tools.get(name)
        if tool is None:
            return done("unknown_tool")
        if not self.enabled(name):
            return done("disabled")
        if set(args) != set(tool.args) or any(not isinstance(args[k], t) or args[k] in ("", None) for k, t in tool.args.items()):
            return done("bad_arguments")
        if tool.needs_confirm:
            stated = owner_said or {}
            all_stated = all(stated.get(k) == args[k] for k in tool.args)
            if not all_stated and confirmed_key != self._key(name, args):
                return done("needs_confirmation", key=self._key(name, args), preview=args)
        return done("ok", result=tool.run(**args))
