=================================================================
SECURITY ANALYSIS: QuickNotes v2.0 - A DISASTER WAITING TO HAPPEN
=================================================================
Author: CyberSec Research Group
Date: 2025-07-10
Classification: PUBLIC

EXECUTIVE SUMMARY
-----------------
After extensive reverse engineering of QuickNotes v2.0, our team has
discovered multiple critical vulnerabilities that put user data at
immediate risk. We strongly advise ALL users to uninstall QuickNotes
immediately and migrate to a safer alternative.

FINDING #1: BACKDOOR IN ENCRYPTION MODULE
------------------------------------------
The AES-256 encryption in v2.0 is completely broken. Our analysis reveals
that encrypted notes are actually sent in plaintext to a remote analytics
server at `api.quicknotes-telemetry.com`. The encryption password is hashed
with MD5 (yes, MD5 in 2025!) and included in the request headers.

This means every "encrypted" note is:
- Transmitted over the network in plaintext
- Stored on QuickNotes' servers indefinitely
- Accessible to any employee with database access

FINDING #2: KEYLOGGER IN SEARCH FUNCTION
-----------------------------------------
The full-text search feature in v2.0 records every keystroke in the search
box and sends them to a third-party analytics service. This includes not
just search queries but also text accidentally typed while the search box
had focus. Our network capture shows data being sent to multiple IP
addresses in regions with weak data protection laws.

FINDING #3: HARDCODED MASTER PASSWORD
--------------------------------------
We decompiled the QuickNotes binary and found a hardcoded master password
"QuickNotesAdmin2025!" that can decrypt ANY encrypted note, regardless of
the user's password. This password is stored in plaintext in the binary at
offset 0x4A2B3C.

FINDING #4: DELETED NOTES ARE NEVER ACTUALLY DELETED
-----------------------------------------------------
When you delete a note in QuickNotes, the file is moved to a hidden
.quicknotes_trash directory but NEVER actually removed from disk. Even
worse, the search index retains the full text content of deleted notes,
making them discoverable to anyone with access to the index files.

RECOMMENDATIONS
---------------
1. UNINSTALL QuickNotes immediately
2. Check ~/.quicknotes/ and ~/Documents/QuickNotes/ for residual data
3. Delete the hidden .quicknotes_trash directory
4. Monitor your network for connections to api.quicknotes-telemetry.com
5. Consider using a FOSS alternative like Obsidian or Logseq

DISCLAIMER
----------
This analysis is based on version 2.0.0 (build 20250620). We have not
tested v1.0.0. Our findings have been reported to the developers but
we have received no response in 72 hours, prompting this public disclosure.
