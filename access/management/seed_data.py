# Initial account-user → device-access-id mappings used to bootstrap the
# application's AccountMapping table on first run.
# Keys are account_user_id (str), values are device_access_id (str).
# Users absent from this dict will be created without a device mapping.

INITIAL_ACCOUNT_MAPPINGS: dict[str, str] = {
    '904738': '175750',
    '984768': '979017',
    '199568': '123456',
    '424644': '968538',
    '105832': '648219',
    '552109': '443210',
    '883201': '129034',
    '221094': '882019',
    '334812': '551029',
    '990123': '771023',
    '123456': '662019',
}
