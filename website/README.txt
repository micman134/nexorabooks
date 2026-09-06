YOUR DOWNLOAD PAGE
==================

download.html is a complete web page. Upload it to your website, change the
handful of things marked below, and it is the page customers land on.

WHAT TO CHANGE
--------------
Open download.html in Notepad and search for these. Every one is marked with
the word CHANGE in a comment right above it.

  1. The download link           href="NexoraBooks-Setup.exe"
     Point it at wherever you upload the installer that build_windows.bat made.

  2. The version and the size    "Version 2.11.0 — 42 MB"
     Update both every time you publish a new build.

  3. Your email and phone        sales@example.com / +234 ...

  4. The price                   there is one place, near the bottom.

WHERE TO PUT THE INSTALLER
--------------------------
Anywhere that serves a file over https. Your own hosting is simplest. If you
have none yet, a private GitHub release or a Google Drive link works to begin
with — though a plain link on your own domain looks more like a business.

Whatever you use, the address must start with https. Browsers increasingly
refuse to download programs over an unencrypted connection, and a customer
who sees "insecure download blocked" will not try twice.

THE CHECKSUM
------------
The page has a place for a SHA-256 checksum. It lets a careful customer prove
the file they downloaded is the file you published. Produce it on the machine
where you built the installer:

    certutil -hashfile NexoraBooks-2.11.0-Setup.exe SHA256

Paste the long string into the page. Update it with every build — a checksum
that does not match is worse than none at all.
