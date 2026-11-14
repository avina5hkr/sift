# Security policy

## Supported versions

Sift is in beta. Security fixes go into the latest release only.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub:
open the repository's **Security** tab and choose **Report a vulnerability**. Include what you
found, how to reproduce it, and what an attacker could do with it.

You should get a reply within a week. Once a fix is released, the advisory is published with
credit to you, unless you prefer otherwise.

## What counts

Sift runs locally, but it has a local web server, talks to a phone with adb, and deletes files,
so these are in scope:

- anything that could delete or change a file without the user's confirmation, or delete a phone
  file whose backup is missing or damaged;
- anything that could write outside the chosen backup folders, or overwrite a backup;
- access to the local API (`127.0.0.1`) from another app, a web page or another user without the
  per-launch token;
- path tricks (symlinks, `..`, odd file names from a phone) that escape a folder;
- data leaving the Mac other than the two downloads the user agrees to (adb and the image model);
- tampering with those downloads that the signature and hash checks miss.

Problems that need someone who already controls your Mac account, or a phone you have unlocked
and authorised for USB debugging, are usually out of scope, but tell us if you are unsure.
