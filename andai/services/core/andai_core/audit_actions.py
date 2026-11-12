"""The registry of audit actions: every action andai writes to `audit_events`.

`ACTIONS` maps each action key to its category and a short label the audit log page shows.
`audit.record` refuses a key that is not here, and `tests/test_audit_actions.py` reads every
`audit.record` call site to check that each action it can write is registered, and that
`andai/docs/audit-actions.md` lists exactly this registry.

Later features add their actions here: every setting Phase 8 adds and every add-on Phase 9 adds
writes an audit event under a key registered in this module, and user erasure (`erasure`) and data
export add their own categories. Keys are never renamed or removed once written, because stored
rows keep them.

`python -m andai_core.audit_actions --markdown` prints the table in `andai/docs/audit-actions.md`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

CATEGORIES = (
    "auth", "members", "settings", "sharing", "meetings", "minutes", "platform", "erasure"
)


@dataclass(frozen=True)
class AuditAction:
    category: str
    label: str


ACTIONS: dict[str, AuditAction] = {
    # auth: signing in, two-factor login and passwords
    "login.success": AuditAction("auth", "Signed in (password accepted)"),
    "login.failure": AuditAction("auth", "Sign-in failed"),
    "mfa.enroll": AuditAction("auth", "Started two-factor enrollment"),
    "mfa.confirm": AuditAction("auth", "Turned on two-factor login"),
    "mfa.disable": AuditAction("auth", "Turned off two-factor login"),
    "mfa.recovery_regenerate": AuditAction("auth", "Generated new recovery codes"),
    "mfa.recovery_use": AuditAction("auth", "Signed in with a recovery code"),
    "mfa.lockout": AuditAction("auth", "Two-factor login locked after wrong codes"),
    "mfa.admin_reset": AuditAction("auth", "Reset a member's two-factor login"),
    "password.reset": AuditAction("auth", "Reset their password"),
    # members: invites, roles, accounts and teams
    "invite.create": AuditAction("members", "Invited a member"),
    "invite.accept": AuditAction("members", "Accepted an invite"),
    "member.role_change": AuditAction("members", "Changed a member's role"),
    "org.owner_transfer": AuditAction("members", "Transferred ownership"),
    "user.disable": AuditAction("members", "Disabled an account"),
    "user.enable": AuditAction("members", "Re-enabled an account"),
    "team.create": AuditAction("members", "Created a team"),
    "team.members_set": AuditAction("members", "Changed a team's members"),
    # settings: org settings
    "org.require_2fa.enable": AuditAction("settings", "Required two-factor login"),
    "org.require_2fa.disable": AuditAction("settings", "Stopped requiring two-factor login"),
    "bot.auto_join.update": AuditAction("settings", "Changed the notetaker's auto-join"),
    # sharing: who else may read a meeting
    "share.grant": AuditAction("sharing", "Shared a meeting"),
    "share.revoke": AuditAction("sharing", "Stopped sharing a meeting"),
    # meetings: the notetaker and the meeting record
    "meeting.dispatch": AuditAction("meetings", "Sent the notetaker"),
    "meeting.stop": AuditAction("meetings", "Stopped the notetaker"),
    "meeting.skip": AuditAction("meetings", "Skipped a scheduled meeting"),
    "meeting.unskip": AuditAction("meetings", "Un-skipped a scheduled meeting"),
    "meeting.delete": AuditAction("meetings", "Deleted a meeting"),
    "meeting.purge": AuditAction("meetings", "Removed a deleted meeting's data"),
    # minutes: the minutes of a meeting
    "mom.edit": AuditAction("minutes", "Edited the minutes"),
    "mom.regenerate": AuditAction("minutes", "Regenerated the minutes"),
    # platform: the org itself and its principal on the meeting platform
    "org.bootstrap": AuditAction("platform", "Created the organisation"),
    "platform_principal.provision": AuditAction("platform", "Provisioned the platform account"),
    "platform_principal.configure": AuditAction("platform", "Configured the platform account"),
    # erasure: a person's data erased (OPS-02)
    "user.erase.request": AuditAction("erasure", "Asked to erase a person's data"),
    "user.erase.complete": AuditAction("erasure", "Erased a person's data"),
    "user.erase.failed": AuditAction("erasure", "Could not finish erasing a person's data"),
}


def is_registered(action: str) -> bool:
    return action in ACTIONS


def markdown() -> str:
    """The registry as the table in `andai/docs/audit-actions.md`, in registry order."""
    lines = [
        "# Audit actions",
        "",
        "Every action andai-core writes to the audit trail (`audit_events`). The list is generated",
        "from `andai_core/audit_actions.py` with",
        "`uv run --directory andai python -m andai_core.audit_actions --markdown`;",
        "`tests/test_audit_actions.py` fails when the two differ.",
        "",
        "| Action | Category | Meaning |",
        "|---|---|---|",
    ]
    lines += [f"| `{key}` | {a.category} | {a.label} |" for key, a in ACTIONS.items()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_core.audit_actions")
    parser.add_argument("--markdown", action="store_true", help="print the docs table")
    args = parser.parse_args(argv)
    if args.markdown:
        print(markdown(), end="")
    else:
        for key, entry in ACTIONS.items():
            print(f"{key}\t{entry.category}\t{entry.label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
