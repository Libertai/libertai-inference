from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer

# Reset/period timestamps come from naive ``TIMESTAMP`` columns (naive UTC on our UTC
# hosts). Serialize them as tz-aware UTC so clients get an unambiguous instant: a naive
# ISO string carries no offset, and JS ``new Date(s)`` parses an offset-less datetime as
# *browser-local* time — which skewed reset countdowns by the client's UTC offset and
# showed "Resets now" while the window was still live.
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(
        lambda dt: (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat(),
        return_type=str,
        when_used="json",
    ),
]
