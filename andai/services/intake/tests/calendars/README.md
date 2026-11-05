# andai/services/intake/tests/calendars

Calendar files used by `test_invite_tz_recur.py`. They are copied unchanged from the test corpus of
the icalendar Python package, version 7.3.0 (`icalendar/tests/` in the wheel installed from PyPI into
`andai/.venv`; project: https://github.com/collective/icalendar), and are used here under its
BSD 2-Clause licence, reproduced below.

| File | From (`icalendar/tests/…`) | Used for |
| --- | --- | --- |
| `issue_237_fail_to_parse_timezone_with_non_ascii_tzid.ics` | `calendars/` | an Outlook-style TZID (`(UTC-03:00) Brasília`) defined only by its VTIMEZONE |
| `america_new_york.ics` | `calendars/` | a custom TZID name with a full VTIMEZONE |
| `timezoned.ics` | `calendars/` | a VTIMEZONE that agrees with its IANA name |
| `issue_112_missing_tzinfo_on_exdate.ics` | `events/` | a Google weekly RRULE with UNTIL and EXDATEs in a TZID (a bare VEVENT; the test wraps it in a VCALENDAR) |
| `issue_1231_recurrence.ics` | `calendars/` | a single instance with RECURRENCE-ID and no series |

## Licence (BSD 2-Clause, from icalendar's `LICENSE.rst`)

Copyright (c) 2012-2013, Plone Foundation

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

- Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
