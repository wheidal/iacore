"""iacore: polite, verified downloading from the Internet Archive (archive.org).

The plumbing shared by deadfetch (DeadShowTagger) and iafetch: one way to ask archive.org for things
(a descriptive User-Agent, downloads not counted as views), a bandwidth cap, resumable downloads checked
against archive.org's size and MD5, and paged search. See README.md for the rules it keeps.
"""

__version__ = "0.1.0"
