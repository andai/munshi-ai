# Audit actions

Every action andai-core writes to the audit trail (`audit_events`). The list is generated
from `andai_core/audit_actions.py` with
`uv run --directory andai python -m andai_core.audit_actions --markdown`;
`tests/test_audit_actions.py` fails when the two differ.

| Action | Category | Meaning |
|---|---|---|
| `login.success` | auth | Signed in (password accepted) |
| `login.failure` | auth | Sign-in failed |
| `mfa.enroll` | auth | Started two-factor enrollment |
| `mfa.confirm` | auth | Turned on two-factor login |
| `mfa.disable` | auth | Turned off two-factor login |
| `mfa.recovery_regenerate` | auth | Generated new recovery codes |
| `mfa.recovery_use` | auth | Signed in with a recovery code |
| `mfa.lockout` | auth | Two-factor login locked after wrong codes |
| `mfa.admin_reset` | auth | Reset a member's two-factor login |
| `password.reset` | auth | Reset their password |
| `invite.create` | members | Invited a member |
| `invite.accept` | members | Accepted an invite |
| `member.role_change` | members | Changed a member's role |
| `org.owner_transfer` | members | Transferred ownership |
| `user.disable` | members | Disabled an account |
| `user.enable` | members | Re-enabled an account |
| `team.create` | members | Created a team |
| `team.members_set` | members | Changed a team's members |
| `org.require_2fa.enable` | settings | Required two-factor login |
| `org.require_2fa.disable` | settings | Stopped requiring two-factor login |
| `bot.auto_join.update` | settings | Changed the notetaker's auto-join |
| `share.grant` | sharing | Shared a meeting |
| `share.revoke` | sharing | Stopped sharing a meeting |
| `meeting.dispatch` | meetings | Sent the notetaker |
| `meeting.stop` | meetings | Stopped the notetaker |
| `meeting.skip` | meetings | Skipped a scheduled meeting |
| `meeting.unskip` | meetings | Un-skipped a scheduled meeting |
| `meeting.delete` | meetings | Deleted a meeting |
| `meeting.purge` | meetings | Removed a deleted meeting's data |
| `mom.edit` | minutes | Edited the minutes |
| `mom.regenerate` | minutes | Regenerated the minutes |
| `org.bootstrap` | platform | Created the organisation |
| `platform_principal.provision` | platform | Provisioned the platform account |
| `platform_principal.configure` | platform | Configured the platform account |
| `user.erase.request` | erasure | Asked to erase a person's data |
| `user.erase.complete` | erasure | Erased a person's data |
| `user.erase.failed` | erasure | Could not finish erasing a person's data |
