from enum import Enum


class Status(str, Enum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    FUNDED = "funded"          # money is in escrow
    DELIVERED = "delivered"    # freelancer says work is done
    RELEASED = "released"      # paid out to freelancer
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


TRANSITIONS: dict[Status, set[Status]] = {
    Status.DRAFT: {Status.AWAITING_PAYMENT},
    Status.AWAITING_PAYMENT: {Status.FUNDED, Status.EXPIRED},
    Status.FUNDED: {Status.DELIVERED, Status.DISPUTED, Status.REFUNDED},
    Status.DELIVERED: {Status.RELEASED, Status.DISPUTED},
    Status.DISPUTED: {Status.RELEASED, Status.REFUNDED},
    Status.RELEASED: set(),
    Status.REFUNDED: set(),
    Status.EXPIRED: set(),
}


class InvalidTransition(Exception):
    pass


def assert_transition(current: str | Status, new: Status) -> None:
    current = Status(current)
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"Cannot move invoice from {current.value} to {new.value}")
