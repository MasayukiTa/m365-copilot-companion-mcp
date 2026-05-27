"""Outlook integration via COM (pywin32).

Lets the agent read inbox previews, send mail, and query / create calendar
events using the user's currently signed-in Outlook profile. No Microsoft
Graph API registration required — auth piggy-backs on whatever Outlook on
the host is already logged into.

Important caveats:
  * The host must have Microsoft Outlook installed and a profile configured.
  * Older Outlook builds may pop a "an application is trying to send mail"
    confirmation dialog when sending; recent M365 Outlook usually does not.
  * Send operations require an unlock per IP just like other write tools.
  * All connections run in the user's identity. The agent has whatever
    mailbox / calendar access that user already has — nothing more.
"""
from datetime import datetime, timedelta
from typing import Optional

from .security import require_unlocked

OL_FOLDER_INBOX = 6
OL_FOLDER_CALENDAR = 9


def _dispatch():
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "pywin32 is required for Outlook COM. Install with pip install pywin32."
        ) from e
    pythoncom.CoInitialize()
    return win32com.client.Dispatch("Outlook.Application")


def _release():
    try:
        import pythoncom  # type: ignore
        pythoncom.CoUninitialize()
    except Exception:
        pass


def outlook_inbox(limit: int = 20, unread_only: bool = False) -> str:
    """Return recent mail headers from the default inbox.

    Args:
        limit: How many messages to return (newest first).
        unread_only: If true, only show unread items.
    """
    try:
        ol = _dispatch()
        try:
            ns = ol.GetNamespace("MAPI")
            inbox = ns.GetDefaultFolder(OL_FOLDER_INBOX)
            items = inbox.Items
            items.Sort("[ReceivedTime]", True)
            rows = []
            for i in range(1, items.Count + 1):
                item = items.Item(i)
                try:
                    if unread_only and not getattr(item, "UnRead", False):
                        continue
                    sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "")
                    subject = getattr(item, "Subject", "(no subject)")
                    received = getattr(item, "ReceivedTime", "")
                    if hasattr(received, "isoformat"):
                        received = received.isoformat(timespec="minutes")
                    unread = "•" if getattr(item, "UnRead", False) else " "
                    rows.append(f"{unread} {str(received):<17}  {str(sender)[:24]:<24}  {str(subject)[:80]}")
                except Exception:
                    continue
                if len(rows) >= limit:
                    break
            if not rows:
                return "(no matching mail items)"
            return "\n".join(rows)
        finally:
            _release()
    except Exception as e:
        return f"[outlook_inbox error: {type(e).__name__}: {e}]"


def outlook_send_mail(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False,
    send_immediately: bool = False,
) -> str:
    """Compose a mail in Outlook. Defaults to saving as a draft for human review.

    Args:
        to: Semicolon-separated recipients (e.g. "alice@x.com; bob@y.com").
        subject: Subject line.
        body: Body text.
        cc: Optional Cc recipients.
        bcc: Optional Bcc recipients.
        html: If true, body is interpreted as HTML.
        send_immediately: If true, send right away. Default false: leave in
            Drafts so the user can review before sending.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        if not to or not subject:
            return "[outlook_send_mail error: 'to' and 'subject' are required]"
        ol = _dispatch()
        try:
            mail = ol.CreateItem(0)  # 0 = MailItem
            mail.To = to
            if cc:
                mail.CC = cc
            if bcc:
                mail.BCC = bcc
            mail.Subject = subject
            if html:
                mail.HTMLBody = body
            else:
                mail.Body = body
            if send_immediately:
                mail.Send()
                return f"Sent: to={to} subject={subject!r}"
            mail.Save()
            return f"Saved as draft: to={to} subject={subject!r} (open Outlook Drafts to review and send)"
        finally:
            _release()
    except Exception as e:
        return f"[outlook_send_mail error: {type(e).__name__}: {e}]"


def outlook_calendar(
    days_ahead: int = 1,
    include_past_today: bool = True,
    limit: int = 50,
) -> str:
    """List calendar events from now (or today) through `days_ahead` days.

    Args:
        days_ahead: How many days into the future to include (1 = today and
            tomorrow's start-of-day).
        include_past_today: If true, include events from start-of-today even
            if their start time has already passed.
        limit: Maximum events to return.
    """
    try:
        ol = _dispatch()
        try:
            ns = ol.GetNamespace("MAPI")
            cal = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            items = cal.Items
            items.Sort("[Start]")
            items.IncludeRecurrences = True

            now = datetime.now()
            window_start = now.replace(hour=0, minute=0, second=0, microsecond=0) if include_past_today else now
            window_end = window_start + timedelta(days=days_ahead + 1)
            fmt = "%m/%d/%Y %I:%M %p"
            restriction = (
                f"[Start] >= '{window_start.strftime(fmt)}' "
                f"AND [Start] < '{window_end.strftime(fmt)}'"
            )
            filtered = items.Restrict(restriction)
            rows = []
            for i in range(1, filtered.Count + 1):
                item = filtered.Item(i)
                try:
                    start = getattr(item, "Start", None)
                    end = getattr(item, "End", None)
                    subject = getattr(item, "Subject", "(no subject)")
                    location = getattr(item, "Location", "") or ""
                    organizer = getattr(item, "Organizer", "") or ""
                    if hasattr(start, "isoformat"):
                        start_s = start.isoformat(timespec="minutes")
                    else:
                        start_s = str(start)
                    if hasattr(end, "isoformat"):
                        end_s = end.isoformat(timespec="minutes")
                    else:
                        end_s = str(end)
                    rows.append(
                        f"{start_s}  →  {end_s}\n"
                        f"  {subject}\n"
                        f"  loc: {location}  / organizer: {organizer}"
                    )
                except Exception:
                    continue
                if len(rows) >= limit:
                    break
            if not rows:
                return "(no events in that window)"
            return "\n\n".join(rows)
        finally:
            _release()
    except Exception as e:
        return f"[outlook_calendar error: {type(e).__name__}: {e}]"


def outlook_create_event(
    subject: str,
    start_iso: str,
    duration_minutes: int = 30,
    location: Optional[str] = None,
    body: Optional[str] = None,
    attendees: Optional[str] = None,
    send_invite: bool = False,
    reminder_minutes: int = 10,
) -> str:
    """Create a calendar event. Defaults to saving locally without sending invites.

    Args:
        subject: Event title.
        start_iso: Start time in ISO 8601 (e.g. "2026-06-01T14:00").
        duration_minutes: Duration in minutes.
        location: Optional location string.
        body: Optional body / agenda text.
        attendees: Semicolon-separated attendee emails (required if send_invite=True).
        send_invite: If true, send meeting invites to attendees. Default false.
        reminder_minutes: Reminder pop-up minutes before start.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        start = datetime.fromisoformat(start_iso)
        ol = _dispatch()
        try:
            appt = ol.CreateItem(1)  # 1 = AppointmentItem
            appt.Subject = subject
            appt.Start = start
            appt.Duration = int(duration_minutes)
            if location:
                appt.Location = location
            if body:
                appt.Body = body
            if attendees:
                appt.MeetingStatus = 1  # olMeeting
                for email in [a.strip() for a in attendees.replace(",", ";").split(";") if a.strip()]:
                    rec = appt.Recipients.Add(email)
                    rec.Type = 1  # required attendee
                appt.Recipients.ResolveAll()
            appt.ReminderMinutesBeforeStart = int(reminder_minutes)
            appt.ReminderSet = True
            if send_invite and attendees:
                appt.Send()
                return f"Sent meeting invite: {subject!r} at {start_iso}"
            appt.Save()
            return f"Saved calendar event: {subject!r} at {start_iso} (open Outlook Calendar to verify)"
        finally:
            _release()
    except ValueError as e:
        return f"[outlook_create_event error: bad start_iso: {e}]"
    except Exception as e:
        return f"[outlook_create_event error: {type(e).__name__}: {e}]"
