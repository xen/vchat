import logging

import bonsai

__all__ = ["authenticate_ldap"]

logger = logging.getLogger(__name__)


async def authenticate_ldap(email: str, password: str, config: dict) -> dict | None:
    """
    Bind with service account, find user DN by email, then verify password with user bind.
    Returns {"email": ..., "name": ...} on success, None on failure.
    """
    server = config["ldap_server"]
    use_ssl = config.get("ldap_use_ssl", False)
    bind_dn = config.get("ldap_bind_dn", "")
    bind_password = config.get("ldap_bind_password", "")
    search_base = config["ldap_search_base"]
    search_filter = config["ldap_search_filter"].format(email=email)
    attr_name = config.get("ldap_attr_name", "displayName")

    service_client = bonsai.LDAPClient(server, tls=use_ssl)
    if bind_dn:
        service_client.set_credentials("SIMPLE", user=bind_dn, password=bind_password)

    try:
        async with service_client.connect(is_async=True) as conn:
            results = await conn.search(
                base=search_base,
                scope=bonsai.LDAPSearchScope.SUB,
                filter_exp=search_filter,
                attrlist=[attr_name],
            )
    except bonsai.LDAPError:
        logger.exception("LDAP service bind or search failed for %s", email)
        return None

    if not results:
        return None

    user_entry = results[0]
    user_dn = str(user_entry.dn)
    name_values = user_entry.get(attr_name, [])
    name = name_values[0] if name_values else email

    user_client = bonsai.LDAPClient(server, tls=use_ssl)
    user_client.set_credentials("SIMPLE", user=user_dn, password=password)

    try:
        async with user_client.connect(is_async=True):
            pass
    except bonsai.AuthenticationError:
        return None
    except bonsai.LDAPError:
        logger.exception("LDAP user bind failed for dn=%s", user_dn)
        return None

    return {"email": email, "name": name}
