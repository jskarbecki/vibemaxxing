from enum import StrEnum


class AccountState(StrEnum):
    OK = "ok"
    NEEDS_LOGIN = "needs_login"
    ERROR = "error"
