import enum


class Role(str, enum.Enum):
    MANAGER = "manager"
    TECHNICIAN = "technician"
    CUSTOMER = "customer"


class TicketStatus(str, enum.Enum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    REPORTED = "reported"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    CLOSED = "closed"


class ContractStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # "expired" is not stored: it follows from end_date


class VisitStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"  # the customer confirmed
    CHANGE_REQUESTED = "change_requested"  # the customer asked for another time
    CANCELLED = "cancelled"
    DONE = "done"


class RepeatUnit(str, enum.Enum):
    DAYS = "days"
    MONTHS = "months"


class InvoiceStatus(str, enum.Enum):
    ISSUED = "issued"
    CANCELLED = "cancelled"  # voided; the number stays used


class InvoiceKind(str, enum.Enum):
    RENEWAL = "renewal"  # contract renewal
    SERVICE = "service"  # periodic service
    REPAIR = "repair"  # repair outside the warranty


class Currency(str, enum.Enum):
    IQD = "IQD"  # Iraqi dinar
    IRR = "IRR"  # rial
    IRT = "IRT"  # toman
