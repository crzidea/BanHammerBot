ADMINS = {"creator", "administrator"}


def has_moderation_rights(member: dict) -> bool:
    return (
        member.get("status") == "administrator"
        and member.get("can_restrict_members") is True
        and member.get("can_delete_messages") is True
    )


def can_manage(member: dict) -> bool:
    return member.get("status") == "creator" or (
        member.get("status") == "administrator" and member.get("can_restrict_members") is True
    )
